let projects = [];

function escapeText(value) {
    const element = document.createElement('span');
    element.textContent = String(value ?? '');
    return element.innerHTML;
}

function projectCountText(project) {
    const stories = Number(project.user_stories_count ?? 0);
    const sprints = Number(project.sprint_count ?? 0);
    return `${stories} ${stories === 1 ? 'story' : 'stories'} / ${sprints} ${sprints === 1 ? 'sprint' : 'sprints'}`;
}

function renderProjects() {
    const container = document.getElementById('projects-grid');
    if (!container) return;
    if (projects.length === 0) {
        container.innerHTML = `
            <div class="col-span-full border-y border-slate-300 py-12 text-center">
                <span class="material-symbols-outlined text-3xl text-slate-400" aria-hidden="true">folder_open</span>
                <p class="mt-3 text-sm font-semibold">No projects yet</p>
            </div>
        `;
        return;
    }
    container.innerHTML = projects.map((project) => `
        <a href="/dashboard/project.html?id=${encodeURIComponent(project.project_id ?? project.id)}"
            class="block min-w-0 rounded-lg border border-slate-300 bg-white p-5 hover:border-slate-500 hover:bg-slate-50 focus:outline-none focus:ring-2 focus:ring-accent">
            <div class="flex min-w-0 items-start justify-between gap-3">
                <div class="min-w-0">
                    <h2 class="break-words text-base font-bold">${escapeText(project.name)}</h2>
                    <p class="mt-1 line-clamp-2 break-words text-sm leading-5 text-slate-600">${escapeText(project.description || 'No description')}</p>
                </div>
                <span class="material-symbols-outlined shrink-0 text-slate-400" aria-hidden="true">arrow_forward</span>
            </div>
            <p class="mt-4 text-xs font-medium text-slate-500">${escapeText(projectCountText(project))}</p>
        </a>
    `).join('');
}

async function readResponse(response, fallback) {
    let payload = {};
    try {
        payload = await response.json();
    } catch (_error) {
        throw new Error(fallback);
    }
    if (response.ok) return payload;
    const detail = payload.detail;
    const message = detail?.error?.message
        || detail?.message
        || (typeof detail === 'string' ? detail : null)
        || fallback;
    throw new Error(message);
}

async function fetchProjects() {
    const container = document.getElementById('projects-grid');
    try {
        const response = await fetch('/api/projects');
        const payload = await readResponse(response, 'Failed to load projects.');
        projects = Array.isArray(payload.data?.items) ? payload.data.items : [];
        renderProjects();
    } catch (error) {
        if (container) {
            container.innerHTML = `<p class="col-span-full border-y border-red-300 bg-red-50 px-4 py-5 text-sm text-red-800">${escapeText(error.message)}</p>`;
        }
    }
}

function setCreateError(message) {
    const error = document.getElementById('create-project-error');
    if (!error) return;
    error.textContent = message;
    error.classList.toggle('hidden', !message);
}

function openCreateProjectModal() {
    const modal = document.getElementById('create-project-modal');
    if (!modal) return;
    modal.classList.remove('hidden');
    modal.classList.add('flex');
    setCreateError('');
    document.getElementById('modal-project-name')?.focus();
}

function closeCreateProjectModal() {
    const modal = document.getElementById('create-project-modal');
    if (!modal) return;
    modal.classList.add('hidden');
    modal.classList.remove('flex');
    setCreateError('');
}

async function submitNewProject() {
    const name = document.getElementById('modal-project-name')?.value?.trim() ?? '';
    const description = document.getElementById('modal-project-description')?.value?.trim() ?? '';
    const repositoryPath = document.getElementById('modal-repository-path')?.value?.trim() ?? '';
    if (!name) {
        setCreateError('Project Name is required.');
        return;
    }
    const button = document.getElementById('btn-submit-project');
    if (button) button.disabled = true;
    setCreateError('');
    try {
        const response = await fetch('/api/projects', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                name,
                description: description || null,
                repository_path: repositoryPath || null,
                idempotency_key: `dashboard-${crypto.randomUUID()}`,
                actor: 'dashboard-ui',
            }),
        });
        const payload = await readResponse(response, 'Project creation failed.');
        const projectId = payload.data?.output?.project_id;
        if (!Number.isInteger(projectId)) {
            throw new Error('Project creation returned no ID.');
        }
        window.location.href = `/dashboard/project.html?id=${projectId}`;
    } catch (error) {
        setCreateError(error.message);
    } finally {
        if (button) button.disabled = false;
    }
}

function installCreateProjectModal() {
    document.getElementById('open-create-project')?.addEventListener('click', openCreateProjectModal);
    document.getElementById('close-create-project')?.addEventListener('click', closeCreateProjectModal);
    document.getElementById('cancel-create-project')?.addEventListener('click', closeCreateProjectModal);
    document.getElementById('create-project-backdrop')?.addEventListener('click', closeCreateProjectModal);
    document.getElementById('create-project-form')?.addEventListener('submit', (event) => {
        event.preventDefault();
        submitNewProject();
    });
    window.addEventListener('keydown', (event) => {
        if (event.key === 'Escape') closeCreateProjectModal();
    });
}

window.addEventListener('DOMContentLoaded', () => {
    installCreateProjectModal();
    fetchProjects();
});
