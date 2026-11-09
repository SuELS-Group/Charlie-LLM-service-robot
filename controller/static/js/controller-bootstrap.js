async function init() {
    bindRobotControlPadEvents();
    try {
        machines = await fetchMachines();
        setServerStatus(true);
        renderMachines(machines);
        startSystemInfoRefresh();
        await refreshDiscoveryServer();

        try {
            const sshStatus = await fetchSSHStatus();
            if (!sshStatus.keys_exist) {
                openSSHSetup();
            }
        } catch (_) {
        }
    } catch (e) {
        console.error('Init error:', e);
        setServerStatus(false);
        document.getElementById('machines-container').innerHTML = `
            <div class="empty-state" style="color:var(--red)">
                Failed to connect to controller server.<br>
                Make sure the server is running on port 8080.
            </div>`;
    }
    setTimeout(initVoiceActivityIndicator, 250);
    initBatteryIndicators();
    setInterval(checkServerHealth, 5000);
}

window.addEventListener('blur', () => {
    if (robotControlMachineId) emergencyStopRobotControl();
});
document.addEventListener('visibilitychange', () => {
    if (document.hidden && robotControlMachineId) emergencyStopRobotControl();
});
document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && robotControlMachineId) closeRobotControl();
});
document.addEventListener('DOMContentLoaded', init);
