def pytest_configure(config):
    config.addinivalue_line("markers", "tool: mark test as tool test")
