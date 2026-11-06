def actions(data):
    """Return named business functions shared by the page and the Agent."""
    def app_info() -> str:
        """Show the status of this application."""
        return "应用已就绪"
    return {"app_info": app_info}
