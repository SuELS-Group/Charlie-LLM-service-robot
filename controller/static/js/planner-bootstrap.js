document.addEventListener('DOMContentLoaded', () => {
    document.getElementById('planner-reconnect-btn')?.addEventListener('click', () => {
        plannerTarget = null;
        plannerReconnectAttempt = 0;
        clearReconnectTimer();
        closePlannerSocket();
        connectPlannerSocket();
    });

    plannerReinitIcons();
    connectPlannerSocket();
    window.setInterval(updateClarificationCountdowns, 1000);
});
