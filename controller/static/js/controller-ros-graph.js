function openROSGraphModal() {
    document.getElementById('ros-graph-modal').classList.remove('hidden');
    reinitIcons();
    loadROSGraph();
}

function closeROSGraphModal() {
    document.getElementById('ros-graph-modal').classList.add('hidden');
}

async function loadROSGraph() {
    const container = document.getElementById('ros-graph-container');
    try {
        const response = await fetch(`${API_BASE}/api/ros_graph/pdf`);
        if (!response.ok) {
            container.innerHTML = `<div class="empty-state" style="color:var(--orange)">
                ${icon('alert-triangle')} No generated graph found.<br>
                <small>Click "Generate" to create a new ROS graph.</small>
            </div>`;
            reinitIcons();
            return;
        }

        const blob = await response.blob();
        const url = URL.createObjectURL(blob);

        container.innerHTML = `<div style="width: 100%; height: 100%; display: flex; align-items: center; justify-content: center;">
            <iframe src="${url}" style="width: 100%; height: 100%; border: none; border-radius: 0.5rem;"></iframe>
        </div>`;
    } catch (e) {
        console.error('Error loading ROS graph:', e);
        container.innerHTML = `<div class="empty-state" style="color:var(--red)">
            ${icon('alert-circle')} Error loading graph: ${e.message}
        </div>`;
        reinitIcons();
    }
}

async function generateROSGraph() {
    const btn = document.getElementById('ros-graph-generate-btn');
    const container = document.getElementById('ros-graph-container');

    if (btn) {
        btn.disabled = true;
        btn.innerHTML = `${icon('loader', 'spin')} Generating...`;
        reinitIcons();
    }

    container.innerHTML = `<div class="loading-state">
        ${icon('loader', 'spin')}
        <span>Generating ROS graph...</span>
    </div>`;
    reinitIcons();

    try {
        const result = await generateROSGraphApi();
        showToast(result.message || 'ROS graph generated successfully', 'success');

        // Wait a moment then reload the graph
        await new Promise(resolve => setTimeout(resolve, 500));
        await loadROSGraph();
    } catch (e) {
        showToast(`Graph generation failed: ${e.message}`, 'error');
        container.innerHTML = `<div class="empty-state" style="color:var(--red)">
            ${icon('alert-circle')} Generation failed: ${e.message}
        </div>`;
        reinitIcons();
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = `${icon('refresh-cw')} Generate`;
            reinitIcons();
        }
    }
}

function downloadROSGraphPDF() {
    const link = document.createElement('a');
    link.href = `${API_BASE}/api/ros_graph/pdf`;
    link.download = 'ros_graph.pdf';
    link.click();
}

