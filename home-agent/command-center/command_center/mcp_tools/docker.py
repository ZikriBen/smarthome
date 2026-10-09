def register(mcp, client):
    @mcp.tool()
    def docker_containers() -> list[dict]:
        """List Docker containers and their state. This is read-only."""
        return client.request("GET", "/docker/containers")

    @mcp.tool()
    def propose_docker_restart(container: str) -> dict:
        """Create, but do not execute, a Docker container restart proposal requiring human approval."""
        return client.request("POST", "/proposals/docker-restart", {"container": container})
