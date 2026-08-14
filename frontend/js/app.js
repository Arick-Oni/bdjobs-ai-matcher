document.addEventListener('DOMContentLoaded', () => {
    console.log('[AI Career Matcher] App initialized.');

    // State Variables
    let allJobs = [];
    let currentFilter = 'all';
    let searchQuery = '';
    let pollingInterval = null;

    // DOM Elements
    const btnFetchMatch = document.getElementById('btn-fetch-match');
    const btnText = document.getElementById('btn-text');
    const btnIcon = document.getElementById('btn-icon');
    const chkFresher = document.getElementById('chk-fresher');
    const txtLastFetch = document.getElementById('txt-last-fetch');

    // Source checkboxes
    const chkSrcBdjobs = document.getElementById('chk-src-bdjobs');
    const chkSrcLinkedin = document.getElementById('chk-src-linkedin');
    const chkSrcRemotive = document.getElementById('chk-src-remotive');
    const lblSrcBdjobs = document.getElementById('lbl-src-bdjobs');
    const lblSrcLinkedin = document.getElementById('lbl-src-linkedin');
    const lblSrcRemotive = document.getElementById('lbl-src-remotive');

    const statusBox = document.getElementById('status-box');
    const statusFill = document.getElementById('status-fill');
    const statusStep = document.getElementById('status-step');
    const statusMsg = document.getElementById('status-msg');

    const cntTotal = document.getElementById('cnt-total');
    const cntHigh = document.getElementById('cnt-high');
    const cntMed = document.getElementById('cnt-med');
    const cntLow = document.getElementById('cnt-low');

    const inputSearch = document.getElementById('input-search');
    const btnClearSearch = document.getElementById('btn-clear-search');

    const tabBtns = document.querySelectorAll('.tab-btn');
    const statCards = document.querySelectorAll('.stat-card');
    const jobsContainer = document.getElementById('jobs-container');
    const emptyState = document.getElementById('empty-state');

    // Modal Elements
    const btnCvModal = document.getElementById('btn-cv-modal');
    const cvModal = document.getElementById('cv-modal');
    const btnCloseCvModal = document.getElementById('btn-close-cv-modal');
    const cvFilename = document.getElementById('cv-filename');
    const cvMeta = document.getElementById('cv-meta');
    const cvPreview = document.getElementById('cv-preview');
    const cvFileInput = document.getElementById('cv-file-input');
    const dropzone = document.getElementById('dropzone');

    // 1. Initialize Application
    initApp();

    function initApp() {
        loadLatestJobs();
        loadCvInfo();
        setupEvents();
    }

    // Load Cached Jobs
    async function loadLatestJobs() {
        try {
            const res = await fetch('/api/latest-jobs');
            if (!res.ok) return;
            const data = await res.json();
            if (data && data.jobs && data.jobs.length > 0) {
                allJobs = data.jobs;
                if (data.fetched_at) {
                    txtLastFetch.innerText = `Last checked: ${data.fetched_at}`;
                }
                updateStats(data);
                renderJobs();
            }
        } catch (err) {
            console.error('Failed to load cached jobs:', err);
        }
    }

    // Load Active CV Metadata
    async function loadCvInfo() {
        try {
            const res = await fetch('/api/cv-info');
            if (!res.ok) return;
            const data = await res.json();
            if (data.has_cv) {
                cvFilename.innerText = data.filename || 'current_cv.pdf';
                cvMeta.innerText = `Size: ${data.size_kb} KB | Active Profile`;
                cvPreview.innerText = data.preview || 'PDF loaded ready for matching.';
            } else {
                cvFilename.innerText = 'No CV Uploaded';
                cvMeta.innerText = 'Please upload a PDF CV';
                cvPreview.innerText = 'Upload a resume PDF to enable AI matching.';
            }
        } catch (err) {
            console.error('Failed to load CV info:', err);
        }
    }

    // Update Counter Badges
    function updateStats(data) {
        cntTotal.innerText = data.total_count || allJobs.length;
        cntHigh.innerText = data.high_count || allJobs.filter(j => j.Match_Level === 'HIGH').length;
        cntMed.innerText = data.medium_count || allJobs.filter(j => j.Match_Level === 'MEDIUM').length;
        cntLow.innerText = data.low_count || allJobs.filter(j => j.Match_Level === 'LOW' || j.Match_Level === 'IRRELEVANT').length;
    }

    function getSelectedSources() {
        const sources = [];
        if (chkSrcBdjobs && chkSrcBdjobs.checked) sources.push('bdjobs');
        if (chkSrcLinkedin && chkSrcLinkedin.checked) sources.push('linkedin');
        if (chkSrcRemotive && chkSrcRemotive.checked) sources.push('remotive');
        return sources.length > 0 ? sources : ['bdjobs', 'linkedin', 'remotive'];
    }

    // Render Filtered Job Cards
    function renderJobs() {
        let filtered = allJobs;

        // Filter by Tab
        if (currentFilter === 'HIGH') {
            filtered = filtered.filter(j => j.Match_Level === 'HIGH');
        } else if (currentFilter === 'MEDIUM') {
            filtered = filtered.filter(j => j.Match_Level === 'MEDIUM');
        } else if (currentFilter === 'LOW') {
            filtered = filtered.filter(j => j.Match_Level === 'LOW' || j.Match_Level === 'IRRELEVANT');
        } else if (currentFilter === 'src-bdjobs') {
            filtered = filtered.filter(j => (j.Source || 'BDJobs').toLowerCase() === 'bdjobs');
        } else if (currentFilter === 'src-linkedin') {
            filtered = filtered.filter(j => (j.Source || '').toLowerCase() === 'linkedin');
        } else if (currentFilter === 'src-remotive') {
            filtered = filtered.filter(j => (j.Source || '').toLowerCase() === 'remotive');
        }

        // Search Query
        if (searchQuery.trim() !== '') {
            const q = searchQuery.toLowerCase();
            filtered = filtered.filter(j => 
                (j.Title && j.Title.toLowerCase().includes(q)) ||
                (j.Company && j.Company.toLowerCase().includes(q)) ||
                (j.Location && j.Location.toLowerCase().includes(q)) ||
                (j.Source && j.Source.toLowerCase().includes(q)) ||
                (j.Match_Reason && j.Match_Reason.toLowerCase().includes(q))
            );
        }

        if (filtered.length === 0) {
            jobsContainer.innerHTML = '';
            jobsContainer.appendChild(emptyState);
            emptyState.classList.remove('hidden');
            return;
        }

        emptyState.classList.add('hidden');
        jobsContainer.innerHTML = '';

        filtered.forEach(job => {
            const card = document.createElement('article');
            card.className = 'job-card';

            const score = job.Match_Score || 0;
            let badgeClass = 'badge-low';
            let badgeIcon = 'fa-thumbs-down';
            if (score >= 70 || job.Match_Level === 'HIGH') {
                badgeClass = 'badge-high';
                badgeIcon = 'fa-fire';
            } else if (score >= 45 || job.Match_Level === 'MEDIUM') {
                badgeClass = 'badge-medium';
                badgeIcon = 'fa-bolt';
            }

            const src = (job.Source || 'BDJobs').toLowerCase();
            let srcBadgeClass = 'source-badge-bdjobs';
            let srcIcon = 'fa-solid fa-briefcase';
            let srcLabel = 'BDJobs';
            let applyBtnClass = '';
            let applyBtnLabel = 'Apply on BDJobs';

            if (src === 'linkedin') {
                srcBadgeClass = 'source-badge-linkedin';
                srcIcon = 'fa-brands fa-linkedin';
                srcLabel = 'LinkedIn BD';
                applyBtnClass = 'btn-linkedin';
                applyBtnLabel = 'Apply on LinkedIn';
            } else if (src === 'remotive') {
                srcBadgeClass = 'source-badge-remotive';
                srcIcon = 'fa-solid fa-globe';
                srcLabel = 'Remotive Tech';
                applyBtnClass = 'btn-remotive';
                applyBtnLabel = 'Apply on Remotive';
            }

            card.innerHTML = `
                <div class="job-card-top">
                    <div class="job-title-group">
                        <div style="display: flex; align-items: center; gap: 8px; margin-bottom: 4px;">
                            <span class="source-badge ${srcBadgeClass}"><i class="${srcIcon}"></i> ${srcLabel}</span>
                        </div>
                        <h3>${escapeHtml(job.Title || 'Untitled Job')}</h3>
                        <div class="company-name">${escapeHtml(job.Company || 'Confidential Company')}</div>
                    </div>
                    <div class="match-badge ${badgeClass}">
                        <i class="fa-solid ${badgeIcon}"></i> ${score}%
                    </div>
                </div>

                <div class="meta-chips">
                    <span class="chip"><i class="fa-solid fa-location-dot"></i> ${escapeHtml(job.Location || 'Bangladesh')}</span>
                    <span class="chip"><i class="fa-solid fa-briefcase"></i> ${escapeHtml(job.Experience || 'Fresher')}</span>
                    <span class="chip"><i class="fa-solid fa-money-bill-wave"></i> ${escapeHtml(job.Salary || 'Negotiable')}</span>
                    ${job.Workplace ? `<span class="chip"><i class="fa-solid fa-laptop-code"></i> ${escapeHtml(job.Workplace)}</span>` : ''}
                </div>

                ${job.Match_Reason ? `
                    <div class="reason-box">
                        <strong>AI Fit Analysis:</strong> ${escapeHtml(job.Match_Reason)}
                    </div>
                ` : ''}

                <div class="card-footer">
                    <span class="deadline-text"><i class="fa-regular fa-clock"></i> ${job.Deadline ? `Deadline: ${escapeHtml(job.Deadline)}` : `Posted: ${escapeHtml(job.Publish_Date || 'Recent')}`}</span>
                    ${job.Job_URL ? `
                        <a href="${job.Job_URL}" target="_blank" rel="noopener noreferrer" class="apply-btn ${applyBtnClass}">
                            ${applyBtnLabel} <i class="fa-solid fa-arrow-up-right-from-square"></i>
                        </a>
                    ` : ''}
                </div>
            `;

            jobsContainer.appendChild(card);
        });
    }

    // Bind All Event Listeners Cleanly
    function setupEvents() {
        // Source Checkbox UI feedback
        [
            { chk: chkSrcBdjobs, lbl: lblSrcBdjobs },
            { chk: chkSrcLinkedin, lbl: lblSrcLinkedin },
            { chk: chkSrcRemotive, lbl: lblSrcRemotive }
        ].forEach(({ chk, lbl }) => {
            if (chk && lbl) {
                chk.addEventListener('change', () => {
                    lbl.classList.toggle('checked', chk.checked);
                });
            }
        });

        // 1. Fetch & Match Button
        if (btnFetchMatch) {
            btnFetchMatch.addEventListener('click', async () => {
                console.log('[Event] Multi-Site Fetch & Match clicked.');
                const selectedSources = getSelectedSources();

                btnFetchMatch.disabled = true;
                btnIcon.className = 'fa-solid fa-spinner fa-spin';
                btnText.innerText = 'Initiating Multi-Site Fetch...';

                statusBox.classList.remove('hidden');
                statusFill.style.width = '10%';
                statusStep.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Starting...';
                statusMsg.innerText = `Fetching live jobs from: ${selectedSources.join(', ')}...`;

                try {
                    const isFresher = chkFresher ? chkFresher.checked : true;
                    const sourcesParam = encodeURIComponent(selectedSources.join(','));
                    const res = await fetch(`/api/match-jobs?is_fresher=${isFresher}&deadline_today=true&sources=${sourcesParam}`, {
                        method: 'POST'
                    });

                    if (!res.ok) {
                        const errData = await res.json();
                        throw new Error(errData.detail || 'Fetch failed');
                    }

                    // Start status polling
                    startStatusPolling();

                } catch (err) {
                    console.error('Matching Error:', err);
                    statusStep.innerText = 'Error!';
                    statusMsg.innerText = err.message || 'Failed to match jobs.';
                    statusFill.style.width = '0%';
                    btnFetchMatch.disabled = false;
                    btnIcon.className = 'fa-solid fa-bolt';
                    btnText.innerText = "Fetch & Match Multi-Site Jobs";
                }
            });
        }

        // 2. Search Box Filter
        if (inputSearch) {
            inputSearch.addEventListener('input', (e) => {
                searchQuery = e.target.value;
                if (btnClearSearch) {
                    btnClearSearch.classList.toggle('hidden', searchQuery === '');
                }
                renderJobs();
            });
        }

        if (btnClearSearch) {
            btnClearSearch.addEventListener('click', () => {
                inputSearch.value = '';
                searchQuery = '';
                btnClearSearch.classList.add('hidden');
                renderJobs();
            });
        }

        // 3. Tab Buttons
        tabBtns.forEach(btn => {
            btn.addEventListener('click', () => {
                tabBtns.forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
                currentFilter = btn.dataset.filter;
                renderJobs();
            });
        });

        // 4. Stat Cards Click
        statCards.forEach(card => {
            card.addEventListener('click', () => {
                statCards.forEach(c => c.classList.remove('active'));
                card.classList.add('active');
                currentFilter = card.dataset.filter;

                tabBtns.forEach(b => {
                    b.classList.toggle('active', b.dataset.filter === currentFilter);
                });
                renderJobs();
            });
        });

        // 5. Modal Control
        if (btnCvModal && cvModal) {
            btnCvModal.addEventListener('click', () => cvModal.classList.remove('hidden'));
        }
        if (btnCloseCvModal && cvModal) {
            btnCloseCvModal.addEventListener('click', () => cvModal.classList.add('hidden'));
        }
        if (cvModal) {
            cvModal.addEventListener('click', (e) => {
                if (e.target === cvModal) cvModal.classList.add('hidden');
            });
        }

        // 6. CV File Upload
        if (cvFileInput) {
            cvFileInput.addEventListener('change', async (e) => {
                const file = e.target.files[0];
                if (!file) return;
                uploadCvFile(file);
            });
        }

        if (dropzone) {
            dropzone.addEventListener('dragover', (e) => {
                e.preventDefault();
                dropzone.style.borderColor = 'var(--primary)';
            });

            dropzone.addEventListener('dragleave', () => {
                dropzone.style.borderColor = 'var(--border-accent)';
            });

            dropzone.addEventListener('drop', (e) => {
                e.preventDefault();
                dropzone.style.borderColor = 'var(--border-accent)';
                if (e.dataTransfer.files && e.dataTransfer.files.length > 0) {
                    uploadCvFile(e.dataTransfer.files[0]);
                }
            });
        }
    }

    // Status Polling for UI feedback during async background tasks
    function startStatusPolling() {
        if (pollingInterval) clearInterval(pollingInterval);
        
        pollingInterval = setInterval(async () => {
            try {
                const res = await fetch('/api/status');
                if (res.ok) {
                    const st = await res.json();
                    statusStep.innerText = st.step || 'Processing...';
                    statusMsg.innerText = st.message || '';

                    if (st.step && st.step.includes('Scraping')) statusFill.style.width = '35%';
                    if (st.step && st.step.includes('AI Matching')) statusFill.style.width = '75%';

                    if (!st.is_running) {
                        stopStatusPolling();
                        statusFill.style.width = '100%';
                        statusStep.innerHTML = '<i class="fa-solid fa-check"></i> Complete!';
                        
                        btnFetchMatch.disabled = false;
                        btnIcon.className = 'fa-solid fa-bolt';
                        btnText.innerText = "Fetch & Match Multi-Site Jobs";

                        // Load updated jobs
                        await loadLatestJobs();
                    }
                }
            } catch (e) {
                console.error('Polling error:', e);
            }
        }, 1500);
    }

    function stopStatusPolling() {
        if (pollingInterval) clearInterval(pollingInterval);
    }

    async function uploadCvFile(file) {
        if (!file.name.toLowerCase().endsWith('.pdf')) {
            alert('Please select a PDF file.');
            return;
        }

        const formData = new FormData();
        formData.append('file', file);

        cvFilename.innerText = 'Uploading...';
        try {
            const res = await fetch('/api/upload-cv', {
                method: 'POST',
                body: formData
            });

            if (!res.ok) throw new Error('Upload failed');
            const data = await res.json();
            alert(`CV '${data.filename}' uploaded successfully!`);
            loadCvInfo();
        } catch (err) {
            alert('Failed to upload CV: ' + err.message);
            loadCvInfo();
        }
    }

    function escapeHtml(str) {
        return String(str)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#039;");
    }
});
