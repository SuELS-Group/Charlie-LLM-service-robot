from types import SimpleNamespace


class QoSProfile:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


qos = SimpleNamespace(QoSProfile=QoSProfile,
    DurabilityPolicy=SimpleNamespace(TRANSIENT_LOCAL='transient_local'),
    ReliabilityPolicy=SimpleNamespace(RELIABLE='reliable'))


class Node:

    def __init__(self, name: str):
        self.name = name
        self._publishers = {}
        self._subscriptions = {}
        self._logger = MockLogger(name)

    def create_publisher(self, msg_type, topic: str, qos: int):
        publisher = MockPublisher(topic, msg_type)
        self._publishers[topic] = publisher
        return publisher

    def create_subscription(self, msg_type, topic: str, callback, qos: int):
        subscription = MockSubscription(topic, msg_type, callback)
        self._subscriptions[topic] = subscription
        return subscription

    def create_service(self, srv_type, name, callback):
        return MockSubscription(name, srv_type, callback)

    def create_timer(self, seconds, callback):
        return MockSubscription('timer', seconds, callback)

    def get_logger(self):
        return self._logger

    def destroy_node(self):
        pass


class MockPublisher:

    def __init__(self, topic: str, msg_type):
        self.topic = topic
        self.msg_type = msg_type
        self.published_messages = []

    def publish(self, msg):
        self.published_messages.append(msg)


class MockSubscription:

    def __init__(self, topic: str, msg_type, callback):
        self.topic = topic
        self.msg_type = msg_type
        self.callback = callback


class MockLogger:

    def __init__(self, name: str):
        self.name = name
        self.logs = []

    def info(self, msg: str):
        self.logs.append(("INFO", msg))
        print(f"[{self.name}] INFO: {msg}")

    def warn(self, msg: str):
        self.logs.append(("WARN", msg))
        print(f"[{self.name}] WARN: {msg}")

    warning = warn

    def error(self, msg: str):
        self.logs.append(("ERROR", msg))
        print(f"[{self.name}] ERROR: {msg}")

    def debug(self, msg: str):
        self.logs.append(("DEBUG", msg))


class MockContext:

    def __init__(self):
        self.ok = True

    def shutdown(self):
        self.ok = False


_context = None


def init(args=None, context=None):
    global _context
    _context = MockContext()


def shutdown(context=None):
    global _context
    if _context:
        _context.shutdown()


def spin_once(node, timeout_sec=None):
    pass


def spin(node):
    pass


def ok(context=None):
    global _context
    return _context and _context.ok


class node:
    Node = Node
