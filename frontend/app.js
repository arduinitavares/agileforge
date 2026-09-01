let projects = [];
let createModalOpener = null;

function escapeText(value) {
    const element = document.createElement('span');
    element.textContent = String(value ?? '');
    return element.innerHTML;
}

function projectCountText(project) {
    const stories = Number(project.user_stories_count ?? 0);
    const sprints = Number(project.sprint_count ?? 0);
    return `${stories} ${stories === 1 ? 'story' : 'stories'} · ${sprints} ${sprints === 1 ? 'sprint' : 'sprints'}`;
}

function renderProjects() {
    const container = document.getElementById('projects-grid');
    if (!container) return;
    if (projects.length === 0) {
        container.innerHTML = `
            <div class="col-span-full rounded-xl border border-dashed border-slate-300 bg-white py-12 text-center">
                <span class="material-symbols-outlined text-3xl text-slate-400" aria-hidden="true">folder_open</span>
                <p class="mt-2 text-sm font-semibold text-slate-800">No project workspaces yet</p>
                <p class="mt-1 text-xs text-slate-500">Create your first project to begin product framing and sprint delivery.</p>
            </div>
        `;
        return;
    }
    container.innerHTML = projects.map((project) => `
        <a href="/dashboard/project.html?id=${encodeURIComponent(project.project_id ?? project.id)}"
            class="group flex flex-col justify-between min-w-0 rounded-xl border border-slate-200 bg-white p-4 shadow-xs hover:border-blue-400 hover:shadow-md transition-all">
            <div>
                <div class="flex items-start justify-between gap-3">
                    <div class="min-w-0">
                        <div class="flex items-center gap-2">
                            <span class="grid size-5 shrink-0 place-items-center rounded bg-blue-50 text-blue-600 font-mono text-[10px] font-bold">#${escapeText(project.project_id ?? project.id)}</span>
                            <h2 class="truncate text-sm font-bold text-slate-900 group-hover:text-blue-600 transition-colors">${escapeText(project.name)}</h2>
                        </div>
                        <p class="mt-1.5 line-clamp-2 text-xs leading-relaxed text-slate-600">${escapeText(project.description || 'No description provided')}</p>
                    </div>
                    <span class="material-symbols-outlined shrink-0 text-slate-300 group-hover:text-blue-600 group-hover:translate-x-0.5 transition-all text-[18px]" aria-hidden="true">arrow_forward</span>
                </div>
            </div>
            <div class="mt-4 flex items-center justify-between border-t border-slate-100 pt-3 text-[11px] text-slate-500 font-medium">
                <span class="font-mono">${escapeText(projectCountText(project))}</span>
                <span class="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] text-slate-600 font-mono">Workspace</span>
            </div>
        </a>
    `).join('');
}

function validationIssueMessage(issue) {
    if (!issue || typeof issue.msg !== 'string') return null;
    const location = (Array.isArray(issue.loc) ? issue.loc : [])
        .filter((part) => part !== 'body')
        .map((part) => String(part).replaceAll('_', ' ').replace(/\b\w/g, (letter) => letter.toUpperCase()))
        .join(' > ');
    const message = issue.msg.trim().replace(/[.]+$/, '');
    return `${location ? `${location}: ` : ''}${message}.`;
}

function responseErrorMessage(payload, fallback) {
    const detail = payload?.detail;
    if (Array.isArray(detail)) {
        const validation = detail.map(validationIssueMessage).filter(Boolean).join(' ');
        if (validation) return validation;
    }
    return detail?.error?.message
        || detail?.message
        || (typeof detail === 'string' ? detail : null)
        || fallback;
}

async function readResponse(response, fallback) {
    let payload = {};
    try {
        payload = await response.json();
    } catch (_error) {
        throw new Error(fallback);
    }
    if (response.ok) return payload;
    throw new Error(responseErrorMessage(payload, fallback));
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
            container.innerHTML = `<p class="col-span-full rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-xs text-red-800 font-mono">${escapeText(error.message)}</p>`;
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
    createModalOpener = document.activeElement;
    const content = document.getElementById('dashboard-content');
    if (content) content.inert = true;
    modal.classList.remove('hidden');
    modal.classList.add('flex');
    setCreateError('');
    document.getElementById('modal-project-name')?.focus();
}

function closeCreateProjectModal() {
    const modal = document.getElementById('create-project-modal');
    if (!modal || modal.classList.contains('hidden')) return;
    modal.classList.add('hidden');
    modal.classList.remove('flex');
    const content = document.getElementById('dashboard-content');
    if (content) content.inert = false;
    setCreateError('');
    const opener = createModalOpener;
    createModalOpener = null;
    opener?.focus();
}

function createModalFocusableElements() {
    const form = document.getElementById('create-project-form');
    if (!form) return [];
    return [...form.querySelectorAll(
        'button:not([disabled]), input:not([disabled]), textarea:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])',
    )].filter((element) => element.getAttribute?.('aria-hidden') !== 'true');
}

function handleCreateModalKeydown(event) {
    const modal = document.getElementById('create-project-modal');
    if (!modal || modal.classList.contains('hidden')) return;
    if (event.key === 'Escape') {
        event.preventDefault();
        closeCreateProjectModal();
        return;
    }
    if (event.key !== 'Tab') return;
    const focusable = createModalFocusableElements();
    if (focusable.length === 0) return;
    const first = focusable[0];
    const last = focusable.at(-1);
    if (!focusable.includes(document.activeElement)) {
        event.preventDefault();
        (event.shiftKey ? last : first).focus();
    } else if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
    }
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
    window.addEventListener('keydown', handleCreateModalKeydown);
}

window.addEventListener('DOMContentLoaded', () => {
    installCreateProjectModal();
    fetchProjects();
});
