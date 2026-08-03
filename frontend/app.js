let projects = [];

window.addEventListener('DOMContentLoaded', fetchProjects);

function escapeText(value) {
    const element = document.createElement('span');
    element.textContent = String(value ?? '');
    return element.innerHTML;
}

async function fetchProjects() {
    const container = document.getElementById('projects-grid');
    try {
        const response = await fetch('/api/projects');
        if (!response.ok) throw new Error('Failed to load projects.');
        const payload = await response.json();
        projects = Array.isArray(payload.data) ? payload.data : [];
        renderProjects();
    } catch (error) {
        if (container) container.textContent = error.message;
    }
}

function renderProjects() {
    const container = document.getElementById('projects-grid');
    if (!container) return;
    if (projects.length === 0) {
        container.innerHTML = '<p class="col-span-full py-10 text-center text-slate-500">No projects.</p>';
        return;
    }
    container.innerHTML = projects.map((project) => `
        <a href="/dashboard/project.html?id=${project.id}"
            class="block border border-slate-200 dark:border-slate-700 p-5 hover:border-slate-500">
            <h2 class="text-lg font-bold break-words">${escapeText(project.name)}</h2>
            <p class="mt-2 text-xs font-mono text-slate-500">Project ${project.id}</p>
        </a>
    `).join('');
}

function openCreateProjectModal() {
    document.getElementById('create-project-modal')?.classList.remove('hidden');
}

function closeCreateProjectModal() {
    document.getElementById('create-project-modal')?.classList.add('hidden');
}

async function submitNewProject() {
    const name = document.getElementById('modal-project-name')?.value?.trim();
    const origin = document.getElementById('modal-project-origin')?.value;
    if (!name || !['greenfield', 'brownfield'].includes(origin)) return;
    const button = document.getElementById('btn-submit-project');
    if (button) button.disabled = true;
    try {
        const response = await fetch('/api/projects', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                name,
                origin,
                idempotency_key: `dashboard-${crypto.randomUUID()}`,
                changed_by: 'dashboard-ui',
            }),
        });
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.detail || 'Project creation failed.');
        const projectId = payload.data?.output?.project_id;
        if (!Number.isInteger(projectId)) throw new Error('Project creation returned no ID.');
        window.location.href = `/dashboard/project.html?id=${projectId}`;
    } catch (error) {
        window.alert(error.message);
    } finally {
        if (button) button.disabled = false;
    }
}
