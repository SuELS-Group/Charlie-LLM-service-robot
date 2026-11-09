class String:

    def __init__(self, data: str = ""):
        self.data = data

    def __repr__(self):
        return f"String(data='{self.data}')"


class msg:
    String = String
