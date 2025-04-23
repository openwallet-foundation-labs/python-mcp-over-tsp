# MCP Demo

## Run the server
In the `server` directory, run the demo MCP server with:
```
uv run server.py
```
This should host the demo MCP server on <http://127.0.0.1:8000/sse>.

## Run the client
For the client, you will need an Anthropic API key, which you can get [here](https://console.anthropic.com/settings/keys). In the `client` directory, create a `.env` file with your Anthropic API key:
```
ANTHROPIC_API_KEY=sk-ant-api03-put-your-private-key-here
```

Then, run the demo MCP client in the `client` directory with:
```
uv run client.py http://127.0.0.1:8000/sse
```
It should list the available MCP tools from the demo MCP server. You should be able to enter a query to prompt it to use these tools.
