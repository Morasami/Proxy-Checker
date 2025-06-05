#!/usr/bin/env python3
"""
GitHub Actions Proxy Checker
Fetches proxy lists, tests them concurrently, and maintains a working proxies file.
Designed to run continuously in GitHub Actions environment.
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
    "max_concurrent_fetches": 25,
    "max_concurrent_tests": 200,
    "cooldown_minutes": 5,
    "user_agent": "Mozilla/5.0 (Linux; GitHub Actions) ProxyChecker/2.0",
    "max_retries_per_proxy": 2,
    "source_fetch_timeout": 20
}

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

class ProxyType:
    """Enum for proxy types"""
    HTTP = "http"
    HTTPS = "https" 
    SOCKS4 = "socks4"
    SOCKS5 = "socks5"

class ProxyTester:
    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None
        self.working_proxies: Set[str] = set()
        self.machine_ip: Optional[str] = None
        
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
        """Get the machine's public IP address for anonymity testing"""
        try:
            async with self.session.get(CONFIG["test_urls"]["http"], timeout=5) as response:
                if response.status == 200:
                    data = await response.json()
                    self.machine_ip = data.get("origin", "").split(",")[0].strip()
                    logger.info(f"Machine IP detected: {self.machine_ip}")
        except Exception as e:
            logger.warning(f"Could not detect machine IP: {e}")
    
    def parse_proxy_url(self, proxy_str: str) -> Optional[Tuple[str, str, str]]:
        """
        Parse various proxy formats and return (ip, port, type)
        Handles: ip:port, http://ip:port, https://ip:port, socks4://ip:port, socks5://ip:port
        """
        proxy_str = proxy_str.strip()
        if not proxy_str:
            return None
            
        # Handle URL format (http://ip:port, socks5://ip:port, etc.)
        if "://" in proxy_str:
            try:
                parsed = urlparse(proxy_str)
                if parsed.hostname and parsed.port:
                    proxy_type = parsed.scheme.lower()
                    if proxy_type in ["http", "https", "socks4", "socks5"]:
                        return parsed.hostname, str(parsed.port), proxy_type
            except:
                pass
        
        # Handle simple ip:port format
        ip_port_match = re.match(r'^(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}):(\d{1,5})$', proxy_str)
        if ip_port_match:
            ip, port = ip_port_match.groups()
            # Validate IP ranges
            if all(0 <= int(octet) <= 255 for octet in ip.split('.')):
                if 1 <= int(port) <= 65535:
                    return ip, port, ProxyType.HTTP  # Default to HTTP for simple format
        
        return None
    
    def extract_proxies_from_text(self, text: str) -> Set[str]:
        """Extract all valid proxy strings from text content"""
        proxies = set()
        lines = text.replace('\r\n', '\n').replace('\r', '\n').split('\n')
        
        for line in lines:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
                
            # Try to parse the line as a proxy
            parsed = self.parse_proxy_url(line)
            if parsed:
                ip, port, proxy_type = parsed
                # Store in standardized format
                proxies.add(f"{ip}:{port}")
        
        return proxies
    
    async def fetch_proxy_list(self, url: str) -> Set[str]:
        """Fetch and parse a single proxy list URL"""
        proxies = set()
        try:
            logger.debug(f"Fetching from: {url}")
            async with self.session.get(
                url, 
                timeout=CONFIG["source_fetch_timeout"],
                ssl=False
            ) as response:
                if response.status == 200:
                    content = await response.text(errors='ignore')
                    
                    # Try to parse as JSON first
                    try:
                        data = json.loads(content)
                        if isinstance(data, list):
                            for item in data:
                                if isinstance(item, str):
                                    parsed = self.parse_proxy_url(item)
                                    if parsed:
                                        proxies.add(f"{parsed[0]}:{parsed[1]}")
                                elif isinstance(item, dict) and "ip" in item and "port" in item:
                                    proxies.add(f"{item['ip']}:{item['port']}")
                        elif isinstance(data, dict) and "data" in data:
                            for item in data["data"]:
                                if isinstance(item, dict) and "ip" in item and "port" in item:
                                    proxies.add(f"{item['ip']}:{item['port']}")
                    except json.JSONDecodeError:
                        # Not JSON, parse as text
                        proxies = self.extract_proxies_from_text(content)
                    
                    if proxies:
                        logger.info(f"Found {len(proxies)} proxies from {url}")
                else:
                    logger.warning(f"Failed to fetch {url}: HTTP {response.status}")
                    
        except asyncio.TimeoutError:
            logger.warning(f"Timeout fetching {url}")
        except Exception as e:
            logger.warning(f"Error fetching {url}: {e}")
            
        return proxies
    
    async def fetch_all_proxy_lists(self, urls: List[str]) -> Set[str]:
        """Fetch all proxy lists concurrently"""
        semaphore = asyncio.Semaphore(CONFIG["max_concurrent_fetches"])
        
        async def fetch_with_semaphore(url):
            async with semaphore:
                return await self.fetch_proxy_list(url)
        
        logger.info(f"Fetching from {len(urls)} proxy sources...")
        tasks = [fetch_with_semaphore(url) for url in urls]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        all_proxies = set()
        for result in results:
            if isinstance(result, set):
                all_proxies.update(result)
            elif isinstance(result, Exception):
                logger.debug(f"Fetch task failed: {result}")
        
        logger.info(f"Total unique proxies collected: {len(all_proxies)}")
        return all_proxies
    
    async def test_proxy_connectivity(self, proxy_ip_port: str) -> Dict:
        """Test a single proxy comprehensively"""
        result = {
            "proxy": proxy_ip_port,
            "working": False,
            "http_working": False,
            "https_working": False,
            "google_working": False,
            "anonymity": "unknown",
            "latency_ms": None,
            "errors": []
        }
        
        proxy_url = f"http://{proxy_ip_port}"
        
        # Test HTTP connectivity
        try:
            start_time = time.perf_counter()
            async with self.session.get(
                CONFIG["test_urls"]["http"],
                proxy=proxy_url,
                timeout=CONFIG["timeout_seconds"]
            ) as response:
                latency = (time.perf_counter() - start_time) * 1000
                result["latency_ms"] = round(latency, 2)
                
                if response.status == 200:
                    result["http_working"] = True
                    data = await response.json()
                    reported_ip = data.get("origin", "").split(",")[0].strip()
                    
                    # Determine anonymity level
                    if self.machine_ip:
                        if reported_ip == self.machine_ip:
                            result["anonymity"] = "transparent"
                        elif reported_ip == proxy_ip_port.split(":")[0]:
                            result["anonymity"] = "anonymous"
                        else:
                            result["anonymity"] = "elite"
                    else:
                        result["anonymity"] = "unknown"
                else:
                    result["errors"].append(f"HTTP test failed: {response.status}")
        except asyncio.TimeoutError:
            result["errors"].append("HTTP test timeout")
        except Exception as e:
            result["errors"].append(f"HTTP test error: {type(e).__name__}")
        
        # Test HTTPS connectivity
        try:
            async with self.session.get(
                CONFIG["test_urls"]["https"],
                proxy=proxy_url,
                timeout=CONFIG["timeout_seconds"],
                ssl=False
            ) as response:
                if response.status == 200:
                    result["https_working"] = True
                else:
                    result["errors"].append(f"HTTPS test failed: {response.status}")
        except asyncio.TimeoutError:
            result["errors"].append("HTTPS test timeout")
        except Exception as e:
            result["errors"].append(f"HTTPS test error: {type(e).__name__}")
        
        # Test Google connectivity (real-world test)
        try:
            async with self.session.get(
                CONFIG["test_urls"]["connectivity"],
                proxy=proxy_url,
                timeout=CONFIG["timeout_seconds"],
                ssl=False
            ) as response:
                if response.status == 204:  # Google returns 204 for generate_204
                    result["google_working"] = True
                else:
                    result["errors"].append(f"Google test failed: {response.status}")
        except asyncio.TimeoutError:
            result["errors"].append("Google test timeout")
        except Exception as e:
            result["errors"].append(f"Google test error: {type(e).__name__}")
        
        # Proxy is considered working if it passes HTTP and Google tests
        result["working"] = result["http_working"] and result["google_working"]
        
        return result
    
    async def test_proxies_batch(self, proxies: List[str]) -> List[Dict]:
        """Test a batch of proxies concurrently"""
        semaphore = asyncio.Semaphore(CONFIG["max_concurrent_tests"])
        
        async def test_with_semaphore(proxy):
            async with semaphore:
                return await self.test_proxy_connectivity(proxy)
        
        tasks = [test_with_semaphore(proxy) for proxy in proxies]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        valid_results = []
        for result in results:
            if isinstance(result, dict):
                valid_results.append(result)
            elif isinstance(result, Exception):
                logger.debug(f"Test task failed: {result}")
        
        return valid_results
    
    def load_proxy_sources(self) -> List[str]:
        """Load proxy source URLs from file"""
        sources = []
        try:
            with open(CONFIG["proxy_sources_file"], 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#'):
                        sources.append(line)
            logger.info(f"Loaded {len(sources)} proxy sources")
        except FileNotFoundError:
            logger.error(f"Proxy sources file {CONFIG['proxy_sources_file']} not found!")
            sys.exit(1)
        except Exception as e:
            logger.error(f"Error reading proxy sources: {e}")
            sys.exit(1)
        return sources
    
    def load_existing_proxies(self) -> Set[str]:
        """Load existing working proxies from output file"""
        proxies = set()
        try:
            with open(CONFIG["output_file"], 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line and self.parse_proxy_url(line):
                        proxies.add(line)
            logger.info(f"Loaded {len(proxies)} existing proxies")
        except FileNotFoundError:
            logger.info("No existing proxies file found, starting fresh")
        except Exception as e:
            logger.warning(f"Error reading existing proxies: {e}")
        return proxies
    
    def save_working_proxies(self, proxies: Set[str]) -> None:
        """Save working proxies to output file"""
        try:
            # Sort proxies for consistent output
            sorted_proxies = sorted(proxies)
            with open(CONFIG["output_file"], 'w', encoding='utf-8') as f:
                for proxy in sorted_proxies:
                    f.write(f"{proxy}\n")
            logger.info(f"Saved {len(proxies)} working proxies to {CONFIG['output_file']}")
        except Exception as e:
            logger.error(f"Error saving proxies: {e}")
    
    async def run_cycle(self) -> None:
        """Run one complete proxy checking cycle"""
        cycle_start = time.time()
        logger.info("=" * 60)
        logger.info("Starting new proxy checking cycle")
        
        # Load proxy sources
        sources = self.load_proxy_sources()
        if not sources:
            logger.error("No proxy sources available!")
            return
        
        # Load existing proxies for re-testing
        existing_proxies = self.load_existing_proxies()
        
        # Fetch new proxies from sources
        fetched_proxies = await self.fetch_all_proxy_lists(sources)
        
        # Combine all proxies (removes duplicates automatically)
        all_proxies = existing_proxies.union(fetched_proxies)
        logger.info(f"Total proxies to test: {len(all_proxies)} "
                   f"(existing: {len(existing_proxies)}, fetched: {len(fetched_proxies)})")
        
        if not all_proxies:
            logger.warning("No proxies to test!")
            return
        
        # Test all proxies
        logger.info("Starting proxy testing...")
        proxies_list = list(all_proxies)
        results = await self.test_proxies_batch(proxies_list)
        
        # Process results
        working_proxies = set()
        failed_count = 0
        
        for result in results:
            if result["working"]:
                working_proxies.add(result["proxy"])
                logger.info(f"✓ {result['proxy']} - "
                           f"Latency: {result['latency_ms']}ms, "
                           f"Anonymity: {result['anonymity']}, "
                           f"HTTPS: {'✓' if result['https_working'] else '✗'}")
            else:
                failed_count += 1
                logger.debug(f"✗ {result['proxy']} - {', '.join(result['errors'])}")
        
        # Save working proxies
        self.save_working_proxies(working_proxies)
        
        # Summary
        cycle_time = time.time() - cycle_start
        logger.info("-" * 60)
        logger.info(f"Cycle completed in {cycle_time:.2f} seconds")
        logger.info(f"Working proxies: {len(working_proxies)}")
        logger.info(f"Failed proxies: {failed_count}")
        logger.info(f"Success rate: {len(working_proxies)/(len(working_proxies)+failed_count)*100:.1f}%")
        
        # Update working proxies set
        self.working_proxies = working_proxies

async def main():
    """Main execution - single cycle for GitHub Actions"""
    logger.info("GitHub Actions Proxy Checker starting...")
    logger.info(f"Configuration: {json.dumps(CONFIG, indent=2)}")
    
    async with ProxyTester() as tester:
        try:
            logger.info("🔄 Running proxy check cycle")
            await tester.run_cycle()
            logger.info("✅ Proxy check completed successfully")
                
        except KeyboardInterrupt:
            logger.info("Received interrupt signal, shutting down...")
        except Exception as e:
            logger.error(f"Unexpected error: {e}")
            raise

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Program interrupted")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        sys.exit(1)
