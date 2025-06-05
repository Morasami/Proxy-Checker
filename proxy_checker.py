#!/usr/bin/env python3
"""
GitHub Actions Proxy Checker
Fetches proxy lists, tests them concurrently, and maintains a working proxies file.
Designed to run continuously (via scheduled GHA) and save proxies in real-time.
"""

import asyncio
import aiohttp
import re
import time
import logging
import json
from datetime import datetime, timezone
from typing import Set, List, Dict, Optional, Tuple
from urllib.parse import urlparse
import sys
import os

# Configuration
CONFIG = {
    "proxy_sources_file": "list.txt",
    "output_file": "proxies.txt",
    "test_urls": {
        "http": "http://httpbin.org/ip",
        "https": "https://httpbin.org/ip",
        "connectivity": "https://www.google.com/generate_204"
    },
    "timeout_seconds": 15,
    "max_concurrent_fetches": 50,
    "max_concurrent_tests": 1500, # You can adjust this based on performance/GHA limits
    "user_agent": "Mozilla/5.0 (Linux; GitHub Actions) ProxyChecker/2.2", # Incremented version
    "source_fetch_timeout": 20,
    "progress_update_interval_seconds": 5, # How often to log progress summary
}

# Set up logging
logging.basicConfig(
    level=logging.INFO, # Change to logging.DEBUG for more verbose output
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

class ProxyType:
    HTTP = "http"
    HTTPS = "https"
    SOCKS4 = "socks4"
    SOCKS5 = "socks5"

class ProgressReporter:
    def __init__(self, total_proxies: int):
        self.total_proxies = total_proxies
        self.tested_count = 0
        self.working_count = 0
        self.dead_count = 0
        self.lock = asyncio.Lock()
        self.last_update_time = 0
        self.start_time = time.time()

    async def update(self, is_working: bool):
        async with self.lock:
            self.tested_count += 1
            if is_working:
                self.working_count += 1
            else:
                self.dead_count += 1

    def display_progress(self):
        current_time = time.time()
        if current_time - self.last_update_time >= CONFIG["progress_update_interval_seconds"] or \
           self.tested_count == self.total_proxies:

            elapsed_time = current_time - self.start_time
            proxies_per_second = self.tested_count / elapsed_time if elapsed_time > 0 else 0

            if self.total_proxies > 0 and self.tested_count > 0 and proxies_per_second > 0: # Added check for proxies_per_second
                estimated_total_time = self.total_proxies / proxies_per_second
                remaining_time = estimated_total_time - elapsed_time
                eta_str = f"ETA: {remaining_time:.0f}s" if remaining_time > 0 else "ETA: ..."
            else:
                eta_str = "ETA: N/A"

            progress_percent = (self.tested_count / self.total_proxies * 100) if self.total_proxies > 0 else 0

            logger.info(
                f"Progress: {self.tested_count}/{self.total_proxies} ({progress_percent:.1f}%) | "
                f"Working: {self.working_count} | Dead: {self.dead_count} | "
                f"Speed: {proxies_per_second:.1f} p/s | {eta_str}"
            )
            self.last_update_time = current_time

class ProxyTester:
    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None
        self.machine_ip: Optional[str] = None
        self.progress_reporter: Optional[ProgressReporter] = None
        self.output_file_lock = asyncio.Lock()
        self.written_proxies_this_run: Set[str] = set()

    async def __aenter__(self):
        connector = aiohttp.TCPConnector(
            limit=CONFIG["max_concurrent_tests"] + CONFIG["max_concurrent_fetches"],
            limit_per_host=50,
            ttl_dns_cache=300,
            use_dns_cache=True,
        )
        timeout = aiohttp.ClientTimeout(total=CONFIG["timeout_seconds"])
        self.session = aiohttp.ClientSession(
            connector=connector,
            timeout=timeout,
            headers={"User-Agent": CONFIG["user_agent"]}
        )
        await self._get_machine_ip()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()

    async def _get_machine_ip(self) -> None:
        try:
            async with self.session.get(CONFIG["test_urls"]["http"], timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status == 200:
                    data = await response.json()
                    self.machine_ip = data.get("origin", "").split(",")[0].strip()
                    logger.info(f"Machine IP detected: {self.machine_ip}")
                else:
                    logger.warning(f"Could not detect machine IP (HTTP {response.status})")
        except Exception as e:
            logger.warning(f"Could not detect machine IP: {type(e).__name__} - {e}")

    def parse_proxy_url(self, proxy_str: str) -> Optional[Tuple[str, str, str]]:
        proxy_str = proxy_str.strip()
        if not proxy_str:
            return None
        if "://" in proxy_str:
            try:
                parsed = urlparse(proxy_str)
                if parsed.hostname and parsed.port:
                    proxy_type = parsed.scheme.lower()
                    if proxy_type in [ProxyType.HTTP, ProxyType.HTTPS, ProxyType.SOCKS4, ProxyType.SOCKS5]:
                        return parsed.hostname, str(parsed.port), proxy_type
            except ValueError:
                logger.debug(f"Could not parse {proxy_str} as URL")
                pass
        
        ip_port_match = re.match(r'^(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}):(\d{1,5})$', proxy_str)
        if ip_port_match:
            ip, port_str = ip_port_match.groups()
            port = int(port_str)
            if all(0 <= int(octet) <= 255 for octet in ip.split('.')) and 1 <= port <= 65535:
                return ip, str(port), ProxyType.HTTP
        return None

    def extract_proxies_from_text(self, text: str) -> Set[str]:
        proxies = set()
        lines = text.replace('\r\n', '\n').replace('\r', '\n').split('\n')
        for line in lines:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            parsed = self.parse_proxy_url(line)
            if parsed:
                proxies.add(f"{parsed[0]}:{parsed[1]}")
        return proxies

    async def fetch_proxy_list(self, url: str) -> Set[str]:
        proxies = set()
        try:
            logger.debug(f"Fetching from: {url}")
            fetch_timeout = aiohttp.ClientTimeout(total=CONFIG["source_fetch_timeout"])
            async with self.session.get(url, timeout=fetch_timeout, ssl=False) as response:
                if response.status == 200:
                    content = await response.text(errors='ignore')
                    try:
                        data = json.loads(content)
                        if isinstance(data, list):
                            for item in data:
                                if isinstance(item, str):
                                    parsed = self.parse_proxy_url(item)
                                    if parsed: proxies.add(f"{parsed[0]}:{parsed[1]}")
                                elif isinstance(item, dict) and "ip" in item and "port" in item:
                                    proxies.add(f"{item['ip']}:{item['port']}")
                        elif isinstance(data, dict) and "data" in data and isinstance(data["data"], list):
                             for item in data["data"]:
                                if isinstance(item, dict) and "ip" in item and "port" in item:
                                    proxies.add(f"{item['ip']}:{item['port']}")
                    except json.JSONDecodeError:
                        proxies.update(self.extract_proxies_from_text(content))
                    
                    if proxies: logger.info(f"Found {len(proxies)} potential proxies from {url}")
                else:
                    logger.warning(f"Failed to fetch {url}: HTTP {response.status}")
        except asyncio.TimeoutError:
            logger.warning(f"Timeout fetching {url}")
        except Exception as e:
            logger.warning(f"Error fetching {url}: {type(e).__name__} - {e}")
        return proxies

    async def fetch_all_proxy_lists(self, urls: List[str]) -> Set[str]:
        semaphore = asyncio.Semaphore(CONFIG["max_concurrent_fetches"])
        async def fetch_with_semaphore(url_task):
            async with semaphore:
                return await self.fetch_proxy_list(url_task)
        
        logger.info(f"Fetching from {len(urls)} proxy sources...")
        tasks = [fetch_with_semaphore(url) for url in urls]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        all_proxies = set()
        for result in results:
            if isinstance(result, set):
                all_proxies.update(result)
            elif isinstance(result, Exception):
                logger.debug(f"Fetch task failed: {result}")
        logger.info(f"Total unique proxies collected from sources: {len(all_proxies)}")
        return all_proxies

    async def _append_working_proxy_to_file(self, proxy_ip_port: str):
        if proxy_ip_port in self.written_proxies_this_run:
            return
        
        async with self.output_file_lock:
            try:
                with open(CONFIG["output_file"], 'a', encoding='utf-8') as f:
                    f.write(f"{proxy_ip_port}\n")
                self.written_proxies_this_run.add(proxy_ip_port)
                logger.debug(f"Appended working proxy {proxy_ip_port} to {CONFIG['output_file']}")
            except Exception as e:
                logger.error(f"Error appending proxy {proxy_ip_port} to file: {e}")

    async def test_proxy_connectivity(self, proxy_ip_port: str) -> Dict:
        result = {"proxy": proxy_ip_port, "working": False, "http_working": False,
                  "https_working": False, "google_working": False, "anonymity": "unknown",
                  "latency_ms": None, "errors": []}
        proxy_url = f"http://{proxy_ip_port}"

        try:
            start_time = time.perf_counter()
            async with self.session.get(CONFIG["test_urls"]["http"], proxy=proxy_url, allow_redirects=False) as response:
                latency = (time.perf_counter() - start_time) * 1000
                result["latency_ms"] = round(latency, 2)
                if response.status == 200:
                    result["http_working"] = True
                    try:
                        data = await response.json()
                        reported_ip = data.get("origin", "").split(",")[0].strip()
                        if self.machine_ip:
                            if reported_ip == self.machine_ip: result["anonymity"] = "transparent"
                            elif proxy_ip_port.split(":")[0] in reported_ip: result["anonymity"] = "anonymous"
                            else: result["anonymity"] = "elite"
                    except Exception as e_json:
                        result["errors"].append(f"HTTP JSON Error: {type(e_json).__name__}")
                        result["http_working"] = False
                else:
                    result["errors"].append(f"HTTP status: {response.status}")
        except asyncio.TimeoutError: result["errors"].append("HTTP Timeout")
        except aiohttp.ClientProxyConnectionError as e_proxy: result["errors"].append(f"Proxy Connect Error: {e_proxy}")
        except aiohttp.ClientError as e_client: result["errors"].append(f"HTTP ClientError: {type(e_client).__name__}")
        except Exception as e: result["errors"].append(f"HTTP Error: {type(e).__name__}")

        if result["http_working"]:
            try:
                async with self.session.get(CONFIG["test_urls"]["https"], proxy=proxy_url, ssl=False, allow_redirects=False) as response:
                    if response.status == 200: result["https_working"] = True
                    else: result["errors"].append(f"HTTPS status: {response.status}")
            except asyncio.TimeoutError: result["errors"].append("HTTPS Timeout")
            except aiohttp.ClientError as e_client: result["errors"].append(f"HTTPS ClientError: {type(e_client).__name__}")
            except Exception as e: result["errors"].append(f"HTTPS Error: {type(e).__name__}")

        if result["http_working"]:
            try:
                async with self.session.get(CONFIG["test_urls"]["connectivity"], proxy=proxy_url, ssl=False, allow_redirects=False) as response:
                    if response.status == 204: result["google_working"] = True
                    else: result["errors"].append(f"Google status: {response.status}")
            except asyncio.TimeoutError: result["errors"].append("Google Timeout")
            except aiohttp.ClientError as e_client: result["errors"].append(f"Google ClientError: {type(e_client).__name__}")
            except Exception as e: result["errors"].append(f"Google Error: {type(e).__name__}")
        
        result["working"] = result["http_working"] and result["google_working"]

        if self.progress_reporter:
            await self.progress_reporter.update(result["working"])
            self.progress_reporter.display_progress()
        
        if result["working"]:
            await self._append_working_proxy_to_file(proxy_ip_port)
            logger.info(f"✓ {result['proxy']} - Latency: {result['latency_ms']:.0f}ms, Anonymity: {result['anonymity']}, HTTPS: {'✓' if result['https_working'] else '✗'}")
        else:
            logger.debug(f"✗ {result['proxy']} - Errors: {', '.join(result['errors'])}")
            
        return result

    async def test_proxies_batch(self, proxies_to_test: List[str]) -> List[Dict]:
        if not proxies_to_test:
            logger.info("No proxies in batch to test.")
            return []

        semaphore = asyncio.Semaphore(CONFIG["max_concurrent_tests"])
        
        async def test_with_semaphore(proxy_ip_port_task: str):
            async with semaphore:
                return await self.test_proxy_connectivity(proxy_ip_port_task)

        tasks = [test_with_semaphore(p) for p in proxies_to_test]
        raw_results = await asyncio.gather(*tasks, return_exceptions=True)
        
        processed_results = []
        for i, res_item in enumerate(raw_results):
            if isinstance(res_item, dict):
                processed_results.append(res_item)
            elif isinstance(res_item, Exception):
                failed_proxy = proxies_to_test[i]
                logger.warning(f"Test task for proxy '{failed_proxy}' failed with exception: {type(res_item).__name__} - {res_item}")
                processed_results.append({"proxy": failed_proxy, "working": False, "errors": [f"Task Exception: {type(res_item).__name__}"]})
        
        return processed_results

    def load_proxy_sources(self) -> List[str]:
        sources = []
        try:
            with open(CONFIG["proxy_sources_file"], 'r', encoding='utf-8') as f:
                sources = [line.strip() for line in f if line.strip() and not line.startswith('#')]
            logger.info(f"Loaded {len(sources)} proxy sources from {CONFIG['proxy_sources_file']}")
        except FileNotFoundError:
            logger.error(f"Proxy sources file {CONFIG['proxy_sources_file']} not found!")
            sys.exit(1)
        except Exception as e:
            logger.error(f"Error reading proxy sources: {e}")
            sys.exit(1)
        return sources

    def _initial_clear_proxies_file(self):
        try:
            with open(CONFIG["output_file"], 'w', encoding='utf-8') as f:
                f.write("")
            self.written_proxies_this_run.clear()
            logger.info(f"Cleared existing proxies file: {CONFIG['output_file']} for new cycle.")
        except Exception as e:
            logger.error(f"Error clearing proxies file {CONFIG['output_file']}: {e}")
    
    async def _sort_and_finalize_proxies_file(self): # <--- MAKE THIS ASYNC
        """Reads the output file, sorts unique proxies, and writes them back."""
        async with self.output_file_lock: # Ensure no writes happen during this
            try:
                current_proxies = set()
                # Standard file I/O can be blocking, consider asyncio.to_thread for large files
                # For now, assuming this is quick enough not to block the loop significantly.
                with open(CONFIG["output_file"], 'r', encoding='utf-8') as f_read:
                    for line in f_read:
                        line = line.strip()
                        if line and self.parse_proxy_url(line): # Validate before adding
                            current_proxies.add(line)
                
                sorted_proxies = sorted(list(current_proxies))
                
                with open(CONFIG["output_file"], 'w', encoding='utf-8') as f_write:
                    for proxy in sorted_proxies:
                        f_write.write(f"{proxy}\n")
                logger.info(f"Finalized and sorted {len(sorted_proxies)} working proxies in {CONFIG['output_file']}")
            except FileNotFoundError:
                logger.info(f"No {CONFIG['output_file']} to sort (likely no working proxies found).")
            except Exception as e:
                logger.error(f"Error sorting/finalizing proxies file: {e}", exc_info=True)


    async def run_cycle(self) -> None:
        cycle_start_time = time.time()
        logger.info("=" * 60)
        logger.info(f"Starting new proxy checking cycle at {datetime.now(timezone.utc).isoformat()}")
        
        self._initial_clear_proxies_file()

        sources = self.load_proxy_sources()
        if not sources: return

        fetched_proxies = await self.fetch_all_proxy_lists(sources)
        if not fetched_proxies:
            logger.warning("No proxies fetched from sources. Ending cycle.")
            return
            
        proxies_to_test_list = list(fetched_proxies)
        logger.info(f"Total unique proxies to test: {len(proxies_to_test_list)}")

        if not proxies_to_test_list: # Additional check if fetched_proxies was empty
            logger.warning("No proxies to test after fetching. Ending cycle.")
            return

        self.progress_reporter = ProgressReporter(total_proxies=len(proxies_to_test_list))
        self.progress_reporter.display_progress()

        all_results = await self.test_proxies_batch(proxies_to_test_list)

        if self.progress_reporter:
            self.progress_reporter.display_progress()

        await self._sort_and_finalize_proxies_file() # <--- AWAIT THIS CALL

        cycle_duration = time.time() - cycle_start_time
        logger.info("-" * 60)
        logger.info(f"Cycle completed in {cycle_duration:.2f} seconds.")
        if self.progress_reporter:
            logger.info(f"Total Proxies Tested: {self.progress_reporter.tested_count}")
            logger.info(f"Working Proxies Found (and saved): {self.progress_reporter.working_count}")
            logger.info(f"Dead Proxies: {self.progress_reporter.dead_count}")
            if self.progress_reporter.tested_count > 0:
                success_rate = (self.progress_reporter.working_count / self.progress_reporter.tested_count) * 100
                logger.info(f"Success Rate: {success_rate:.1f}%")
        logger.info(f"Working proxies are in {CONFIG['output_file']}")
        logger.info("=" * 60)

async def main():
    logger.info(f"GitHub Actions Proxy Checker v{CONFIG['user_agent'].split('/')[-1]} starting...") # Dynamic version from UA
    logger.info(f"Full Configuration: {json.dumps(CONFIG, indent=2)}")
    
    async with ProxyTester() as tester:
        try:
            await tester.run_cycle()
            logger.info("Proxy check cycle completed successfully.")
        except KeyboardInterrupt:
            logger.info("Received interrupt signal, shutting down...")
        except Exception as e:
            logger.error(f"Unexpected error in main execution: {type(e).__name__} - {e}", exc_info=True)
            raise

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Program interrupted by user.")
    except Exception as e:
        logger.error(f"Fatal error at top level: {type(e).__name__} - {e}", exc_info=True)
        sys.exit(1)
