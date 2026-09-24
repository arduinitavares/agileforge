export function dashboardBundle(overrides = {}) {
    const names = [
        'project', 'position', 'vision', 'goal', 'specification', 'repository',
        'backlogReview', 'roadmapReview', 'acceptedRoadmap', 'storyReviews',
        'sprintPlanReview', 'storyPending', 'storyDependencies', 'sprintCandidates',
        'sprintStatusResponse', 'sprintHistory',
    ];
    return {
        status: 'success',
        data: {
            ...Object.fromEntries(names.map((name) => [name, { status: 200, body: { data: {} } }])),
            ...overrides,
        },
    };
}
