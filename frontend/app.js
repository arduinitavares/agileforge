const FALLBACK_WORKFLOW_STEPS = [
    { id: 'setup', label: 'Project Setup', states: ['SETUP_REQUIRED'] },
    { id: 'vision', label: 'Vision', states: ['VISION_INTERVIEW', 'VISION_REVIEW', 'VISION_PERSISTENCE'] },
    { id: 'backlog', label: 'Backlog', states: ['BACKLOG_INTERVIEW', 'BACKLOG_REVIEW', 'BACKLOG_PERSISTENCE'] },
    { id: 'roadmap', label: 'Roadmap', states: ['ROADMAP_INTERVIEW', 'ROADMAP_REVIEW', 'ROADMAP_PERSISTENCE'] },
    { id: 'story', label: 'Stories', states: ['STORY_INTERVIEW', 'STORY_REVIEW', 'STORY_PERSISTENCE'] },
    {
        id: 'sprint',
        label: 'Sprint',
        states: ['SPRINT_SETUP', 'SPRINT_DRAFT', 'SPRINT_PERSISTENCE', 'SPRINT_VIEW', 'SPRINT_LIST', 'SPRINT_UPDATE_STORY', 'SPRINT_MODIFY', 'SPRINT_COMPLETE'],
    },
];

let dashboardConfig = null;

window.addEventListener('DOMContentLoaded', async () => {
    await fetchDashboardConfig();
    await fetchProjects();
});

async function fetchDashboardConfig() {
    try {
        const response = await fetch('/api/dashboard/config');
        const data = await response.json();
        dashboardConfig = data?.status === 'success' ? data.data : null;
    } catch (error) {
        console.error('Error fetching dashboard config:', error);
        dashboardConfig = null;
    }
}

function getWorkflowSteps() {
    return Array.isArray(dashboardConfig?.workflow_steps) && dashboardConfig.workflow_steps.length > 0
        ? dashboardConfig.workflow_steps
        : FALLBACK_WORKFLOW_STEPS;
}

function normalizeStateKey(value) {
    if (typeof value !== 'string') return 'SETUP_REQUIRED';
    return value.trim().toUpperCase() || 'SETUP_REQUIRED';
}

function getPhaseIdForState(stateKey) {
    const step = getWorkflowSteps().find((item) => Array.isArray(item.states) && item.states.includes(stateKey));
    return step ? step.id : 'setup';
}

function getBadgeMeta(stateKey) {
    const stepId = getPhaseIdForState(stateKey);
    if (stepId === 'setup') return { icon: 'settings', color: 'bg-amber-100/50 text-amber-700 ring-amber-200' };
    if (stepId === 'vision') return { icon: 'visibility', color: 'bg-sky-100/50 text-sky-700 ring-sky-200' };
    if (stepId === 'backlog') return { icon: 'format_list_bulleted', color: 'bg-indigo-100/50 text-indigo-700 ring-indigo-200' };
    if (stepId === 'roadmap') return { icon: 'timeline', color: 'bg-violet-100/50 text-violet-700 ring-violet-200' };
    if (stepId === 'story') return { icon: 'description', color: 'bg-amber-100/50 text-amber-700 ring-amber-200' };
    return { icon: 'bolt', color: 'bg-emerald-100/50 text-emerald-700 ring-emerald-200' };
}

async function fetchProjects() {
    try {
        const response = await fetch('/api/projects');
        const data = await response.json();
        if (data.status === 'success') {
            renderProjects(Array.isArray(data.data) ? data.data : []);
        }
    } catch (error) {
        console.error('Error fetching projects:', error);
    }
}

function renderProjects(projects) {
    const container = document.getElementById('projects-grid');
    if (!container) return;

    container.innerHTML = '';

    if (projects.length === 0) {
        container.innerHTML = `
            <div class="col-span-1 md:col-span-2 lg:col-span-3 text-center p-12 bg-white dark:bg-slate-800/50 border border-slate-200 dark:border-slate-700 rounded-xl">
                <div class="bg-slate-100 dark:bg-slate-800 w-16 h-16 rounded-full flex items-center justify-center mx-auto mb-4">
                    <span class="material-symbols-outlined text-4xl text-slate-400">inbox</span>
                </div>
                <h3 class="text-xl font-bold mb-1">No Projects Found</h3>
                <p class="text-slate-500">Create a new project to start the agentic workflow pipeline.</p>
                <button onclick="openCreateProjectModal()" class="mt-6 px-5 py-2.5 rounded-lg bg-primary text-white font-bold hover:bg-primary/90 transition-all shadow-sm">
                    Create New Project
                </button>
            </div>
        `;
        return;
    }

    projects.forEach((project) => {
        const stateKey = normalizeStateKey(project.fsm_state);
        const badge = getBadgeMeta(stateKey);

        container.innerHTML += `
            <a href="/dashboard/project.html?id=${project.id}" class="block bg-white dark:bg-slate-800/50 border border-slate-200 dark:border-slate-700 p-6 rounded-xl hover:border-primary/50 hover:shadow-lg hover:-translate-y-1 transition-all group">
                <div class="flex justify-between items-start mb-5">
                    <div class="bg-primary/10 w-10 h-10 flex items-center justify-center rounded-lg text-primary transition-colors group-hover:bg-primary group-hover:text-white">
                        <span class="material-symbols-outlined">${badge.icon}</span>
                    </div>
                    <span class="px-3 py-1 rounded-full text-[10px] font-bold ring-1 ring-inset ${badge.color} tracking-wide uppercase">
                        ${stateKey.replace(/_/g, ' ')}
                    </span>
                </div>
                <h3 class="text-xl font-bold mb-2 group-hover:text-primary transition-colors">${project.name}</h3>
                <p class="text-sm text-slate-500 dark:text-slate-400 line-clamp-2 h-10 mb-6">${project.summary || 'No description provided.'}</p>
                
                <div class="flex items-center justify-between border-t border-slate-100 dark:border-slate-700/50 pt-4">
                    <span class="text-xs font-semibold text-slate-400">ID: ${project.id}</span>
                    <div class="flex items-center gap-3">
                        <button onclick="deleteProject(event, ${project.id})" aria-label="Delete project" class="text-slate-400 hover:text-red-500 transition-colors p-1 rounded-full hover:bg-red-50 dark:hover:bg-red-500/10 flex items-center justify-center" title="Delete Project">
                            <span class="material-symbols-outlined text-[18px]">delete</span>
                        </button>
                        <span class="text-primary text-sm font-bold flex items-center gap-1">
                            Open <span class="material-symbols-outlined text-[16px] group-hover:translate-x-1 transition-transform">arrow_forward</span>
                        </span>
                    </div>
                </div>
            </a>
        `;
    });
}

// Modal Handling
function openCreateProjectModal() {
    const modal = document.getElementById('create-project-modal');
    if (modal) {
        modal.classList.remove('hidden');
        document.getElementById('modal-project-name')?.focus();
    }
}

function closeCreateProjectModal() {
    const modal = document.getElementById('create-project-modal');
    if (modal) {
        modal.classList.add('hidden');
        document.getElementById('modal-project-name').value = '';
        document.getElementById('modal-spec-path').value = '';
    }
}

async function submitNewProject(event) {
    if (event) {
        event.preventDefault();
    }
    const nameInput = document.getElementById('modal-project-name');
    const specInput = document.getElementById('modal-spec-path');

    const projectName = nameInput?.value?.trim() || '';
    const specFilePath = specInput?.value?.trim() || '';

    if (!projectName) {
        alert('Please enter a project name.');
        return;
    }

    if (!specFilePath) {
        alert('Please enter a specification file path.');
        return;
    }

    const btn = document.getElementById('btn-submit-project');
    const original = btn?.innerHTML;
    if (btn) {
        btn.innerHTML = '<span class="material-symbols-outlined text-sm animate-spin">cycle</span> Creating...';
        btn.disabled = true;
    }

    try {
        const response = await fetch('/api/projects', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name: projectName, spec_file_path: specFilePath }),
        });

        const data = await response.json();
        if (data.status === 'success' && data.data?.id) {
            closeCreateProjectModal();
            // Automatically push them to the new dedicated FSM page!
            window.location.href = `/dashboard/project.html?id=${data.data.id}`;
        } else {
            alert(data.detail || 'Failed to create project.');
        }
    } catch (error) {
        console.error(error);
        alert('Network error while creating project.');
    } finally {
        if (btn) {
            btn.innerHTML = original;
            btn.disabled = false;
        }
    }
}

async function deleteProject(event, projectId) {
    if (event) {
        event.preventDefault(); // Prevent navigating to project.html
        event.stopPropagation();
    }

    if (!confirm('Are you sure you want to delete this project? This will permanently delete the specification, stories, and all AI generated data.')) {
        return;
    }

    try {
        const response = await fetch(`/api/projects/${projectId}`, {
            method: 'DELETE',
        });
        const data = await response.json();
        if (data.status === 'success') {
            await fetchProjects();
        } else {
            alert(data.detail || 'Failed to delete project.');
        }
    } catch (error) {
        console.error('Error deleting project:', error);
        alert('Network error while deleting project.');
    }
}

// Attach globally
window.openCreateProjectModal = openCreateProjectModal;
window.closeCreateProjectModal = closeCreateProjectModal;
window.submitNewProject = submitNewProject;
window.deleteProject = deleteProject;
