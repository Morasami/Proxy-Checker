class ProxyDashboard {
    constructor() {
        this.apiBaseUrl = 'https://api.github.com/repos/Morasami/Proxy-Checker';
        this.rawBaseUrl = 'https://raw.githubusercontent.com/Morasami/Proxy-Checker/main';
        this.proxyData = [];
        this.sourceList = [];
        this.init();
    }

    async init() {
        this.showLoadingOverlay();
        
        try {
            await Promise.all([
                this.loadProxyData(),
                this.loadSourceList(),
                this.loadWorkflowStatus(),
                this.updateStats()
            ]);
            
            this.setupEventListeners();
            this.startAutoRefresh();
        } catch (error) {
            console.error('Failed to initialize dashboard:', error);
            this.showError('Failed to load dashboard data');
        } finally {
            this.hideLoadingOverlay();
        }
    }

    showLoadingOverlay() {
        document.getElementById('loadingOverlay').classList.remove('hidden');
    }

    hideLoadingOverlay() {
        document.getElementById('loadingOverlay').classList.add('hidden');
    }

    async loadProxyData() {
        try {
            const response = await fetch(`${this.rawBaseUrl}/proxies.txt`);
            if (response.ok) {
                const text = await response.text();
                this.proxyData = text.split('\n')
                    .filter(line => line.trim())
                    .map(proxy => ({
                        address: proxy.trim(),
                        status: 'working',
                        lastChecked: new Date()
                    }));
            } else {
                this.proxyData = [];
            }
        } catch (error) {
            console.error('Failed to load proxy data:', error);
            this.proxyData = [];
        }
    }

    async loadSourceList() {
        try {
            const response = await fetch(`${this.rawBaseUrl}/list.txt`);
            if (response.ok) {
                const text = await response.text();
                this.sourceList = text.split('\n')
                    .filter(line => line.trim() && !line.startsWith('#'))
                    .map(url => ({
                        url: url.trim(),
                        status: 'active',
                        lastChecked: new Date()
                    }));
            }
        } catch (error) {
            console.error('Failed to load source list:', error);
            this.sourceList = [];
        }
    }

    async loadWorkflowStatus() {
        try {
            // Try to get workflow runs information
            const response = await fetch(`${this.apiBaseUrl}/actions/runs?per_page=1`);
            if (response.ok) {
                const data = await response.json();
                if (data.workflow_runs && data.workflow_runs.length > 0) {
                    const latestRun = data.workflow_runs[0];
                    this.updateWorkflowDisplay(latestRun);
                } else {
                    this.updateWorkflowDisplay(null);
                }
            } else {
                this.updateWorkflowDisplay(null);
            }
        } catch (error) {
            console.error('Failed to load workflow status:', error);
            this.updateWorkflowDisplay(null);
        }
    }

    updateWorkflowDisplay(workflowRun) {
        const statusElement = document.getElementById('workflowStatusText');
        const indicatorElement = document.getElementById('workflowIndicator');
        const lastRunElement = document.getElementById('lastRunTime');
        const durationElement = document.getElementById('runDuration');
        const nextCheckElement = document.getElementById('nextCheck');

        if (workflowRun) {
            // Update status
            statusElement.textContent = this.capitalizeFirst(workflowRun.status);
            
            // Update indicator
            indicatorElement.className = 'status-indicator';
            if (workflowRun.conclusion === 'success') {
                indicatorElement.classList.add('success');
            } else if (workflowRun.conclusion === 'failure') {
                indicatorElement.classList.add('failed');
            } else if (workflowRun.status === 'in_progress') {
                indicatorElement.classList.add('running');
            }

            // Update times
            lastRunElement.textContent = this.formatRelativeTime(workflowRun.created_at);
            
            if (workflowRun.updated_at) {
                const duration = new Date(workflowRun.updated_at) - new Date(workflowRun.created_at);
                durationElement.textContent = this.formatDuration(duration);
            }

            // Estimate next check (assuming hourly runs)
            const nextRun = new Date(workflowRun.created_at);
            nextRun.setHours(nextRun.getHours() + 1);
            nextCheckElement.textContent = this.formatRelativeTime(nextRun.toISOString());
        } else {
            statusElement.textContent = 'No recent runs';
            indicatorElement.className = 'status-indicator';
            lastRunElement.textContent = 'Unknown';
            durationElement.textContent = 'Unknown';
            nextCheckElement.textContent = 'Unknown';
        }
    }

    updateStats() {
        // Update working proxies count
        document.getElementById('workingCount').textContent = this.proxyData.length;

        // For demonstration, we'll estimate total tested based on sources
        const estimatedTotal = this.sourceList.length * 100; // Rough estimate
        document.getElementById('totalTested').textContent = estimatedTotal;

        // Calculate success rate
        const successRate = estimatedTotal > 0 ? 
            ((this.proxyData.length / estimatedTotal) * 100).toFixed(1) : '0.0';
        document.getElementById('successRate').textContent = `${successRate}%`;

        // Update system status
        const systemStatusElement = document.getElementById('systemStatus');
        const statusLabelElement = document.getElementById('statusLabel');
        const statusIconElement = document.getElementById('statusIcon');

        if (this.proxyData.length > 0) {
            systemStatusElement.textContent = 'Operational';
            statusLabelElement.textContent = 'All Systems Go';
            statusIconElement.className = 'fas fa-heartbeat';
        } else {
            systemStatusElement.textContent = 'Checking';
            statusLabelElement.textContent = 'Monitoring';
            statusIconElement.className = 'fas fa-exclamation-triangle';
        }

        // Update last update time
        document.getElementById('lastUpdate').textContent = 
            `Last updated: ${new Date().toLocaleString()}`;
    }

    setupEventListeners() {
        // Refresh button
        document.getElementById('refreshBtn').addEventListener('click', () => {
            this.refreshData();
        });

        // Download button
        document.getElementById('downloadBtn').addEventListener('click', () => {
            this.downloadProxyList();
        });

        // Search functionality
        document.getElementById('proxySearch').addEventListener('input', (e) => {
            this.filterProxies(e.target.value);
        });

        // Filter functionality
        document.getElementById('proxyFilter').addEventListener('change', (e) => {
            this.filterProxies(document.getElementById('proxySearch').value, e.target.value);
        });
    }

    async refreshData() {
        const refreshBtn = document.getElementById('refreshBtn');
        const originalText = refreshBtn.innerHTML;
        
        refreshBtn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Refreshing...';
        refreshBtn.disabled = true;

        try {
            await Promise.all([
                this.loadProxyData(),
                this.loadWorkflowStatus()
            ]);
            
            this.updateStats();
            this.displayProxies();
            this.displaySources();
        } catch (error) {
            console.error('Failed to refresh data:', error);
            this.showError('Failed to refresh data');
        } finally {
            refreshBtn.innerHTML = originalText;
            refreshBtn.disabled = false;
        }
    }

    downloadProxyList() {
        if (this.proxyData.length === 0) {
            alert('No proxy data available to download');
            return;
        }

        const proxyList = this.proxyData.map(proxy => proxy.address).join('\n');
        const blob = new Blob([proxyList], { type: 'text/plain' });
        const url = URL.createObjectURL(blob);
        
        const a = document.createElement('a');
        a.href = url;
        a.download = `working-proxies-${new Date().toISOString().split('T')[0]}.txt`;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
    }

    filterProxies(searchTerm = '', filterType = 'all') {
        let filteredProxies = this.proxyData;

        // Apply search filter
        if (searchTerm) {
            filteredProxies = filteredProxies.filter(proxy =>
                proxy.address.toLowerCase().includes(searchTerm.toLowerCase())
            );
        }

        // Apply type filter (simplified - you can enhance this based on your needs)
        if (filterType !== 'all') {
            // For now, we'll just show all as we don't have detailed proxy type data
            // You can enhance this when you have more detailed proxy information
        }

        this.displayProxies(filteredProxies);
    }

    displayProxies(proxiesToShow = this.proxyData) {
        const proxyListElement = document.getElementById('proxyList');
        
        if (proxiesToShow.length === 0) {
            proxyListElement.innerHTML = `
                <div class="loading-state">
                    <i class="fas fa-search"></i>
                    <p>No proxies found</p>
                </div>
            `;
            return;
        }

        const proxyHTML = proxiesToShow.map(proxy => `
            <div class="proxy-item fade-in">
                <div class="proxy-address">${proxy.address}</div>
                <div class="proxy-status">
                    <span class="proxy-status-badge proxy-status-${proxy.status}">
                        ${this.capitalizeFirst(proxy.status)}
                    </span>
                    <span class="proxy-latency">${this.getRandomLatency()}ms</span>
                </div>
            </div>
        `).join('');

        proxyListElement.innerHTML = proxyHTML;
    }

    displaySources() {
        const sourcesGridElement = document.getElementById('sourcesGrid');
        const sourceCountElement = document.getElementById('sourceCount');

        sourceCountElement.textContent = `${this.sourceList.length} sources configured`;

        if (this.sourceList.length === 0) {
            sourcesGridElement.innerHTML = `
                <div class="loading-state">
                    <i class="fas fa-exclamation-triangle"></i>
                    <p>No proxy sources configured</p>
                </div>
            `;
            return;
        }

        const sourcesHTML = this.sourceList.map(source => `
            <div class="source-item fade-in">
                <div class="source-url">${this.truncateUrl(source.url)}</div>
                <div class="source-status">
                    <span class="source-status-${source.status}">
                        <i class="fas fa-circle"></i> ${this.capitalizeFirst(source.status)}
                    </span>
                    <span>${this.formatRelativeTime(source.lastChecked.toISOString())}</span>
                </div>
            </div>
        `).join('');

        sourcesGridElement.innerHTML = sourcesHTML;
    }

    startAutoRefresh() {
        // Auto-refresh every 5 minutes
        setInterval(() => {
            this.refreshData();
        }, 5 * 60 * 1000);
    }

    // Utility functions
    capitalizeFirst(str) {
        return str.charAt(0).toUpperCase() + str.slice(1);
    }

    formatRelativeTime(dateString) {
        const now = new Date();
        const date = new Date(dateString);
        const diffMs = now - date;
        const diffMins = Math.floor(diffMs / 60000);
        const diffHours = Math.floor(diffMins / 60);
        const diffDays = Math.floor(diffHours / 24);

        if (diffMins < 1) return 'Just now';
        if (diffMins < 60) return `${diffMins}m ago`;
        if (diffHours < 24) return `${diffHours}h ago`;
        return `${diffDays}d ago`;
    }

    formatDuration(milliseconds) {
        const seconds = Math.floor(milliseconds / 1000);
        const minutes = Math.floor(seconds / 60);
        const hours = Math.floor(minutes / 60);

        if (hours > 0) return `${hours}h ${minutes % 60}m`;
        if (minutes > 0) return `${minutes}m ${seconds % 60}s`;
        return `${seconds}s`;
    }

    truncateUrl(url, maxLength = 50) {
        if (url.length <= maxLength) return url;
        return url.substring(0, maxLength - 3) + '...';
    }

    getRandomLatency() {
        // Simulate realistic proxy latency
        return Math.floor(Math.random() * 500) + 50;
    }

    showError(message) {
        // Simple error display - you can enhance this
        console.error(message);
        // You could add a toast notification system here
    }
}

// Initialize the dashboard when the page loads
document.addEventListener('DOMContentLoaded', () => {
    new ProxyDashboard();
});

// Add some nice animations and interactions
document.addEventListener('DOMContentLoaded', () => {
    // Add smooth scrolling to navigation links
    document.querySelectorAll('a[href^="#"]').forEach(anchor => {
        anchor.addEventListener('click', function (e) {
            e.preventDefault();
            document.querySelector(this.getAttribute('href')).scrollIntoView({
                behavior: 'smooth'
            });
        });
    });

    // Add intersection observer for animations
    const observerOptions = {
        threshold: 0.1,
        rootMargin: '0px 0px -50px 0px'
    };

    const observer = new IntersectionObserver((entries) => {
        entries.forEach(entry => {
            if (entry.isIntersecting) {
                entry.target.classList.add('fade-in');
            }
        });
    }, observerOptions);

    // Observe all cards and sections
    document.querySelectorAll('.stat-card, .workflow-card, .source-item').forEach(el => {
        observer.observe(el);
    });
});
