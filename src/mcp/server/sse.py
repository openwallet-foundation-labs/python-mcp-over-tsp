"""
SSE Server Transport Module

This module implements a Server-Sent Events (SSE) transport layer for MCP servers.

Example usage:
```
    # Create an SSE transport at an endpoint
    sse = SseServerTransport("/messages/")

    # Create Starlette routes for SSE and message handling
    routes = [
        Route("/sse", endpoint=handle_sse),
        Mount("/messages/", app=sse.handle_post_message),
    ]

    # Define handler functions
    async def handle_sse(request):
        async with sse.connect_sse(
            request.scope, request.receive, request._send
        ) as streams:
            await app.run(
                streams[0], streams[1], app.create_initialization_options()
            )

    # Create and run Starlette app
    starlette_app = Starlette(routes=routes)
    uvicorn.run(starlette_app, host="0.0.0.0", port=port)
```

See SseServerTransport class documentation for more details.
"""

import base64
import json
import logging
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import quote
from uuid import uuid4

import anyio
import requests
import tsp_python as tsp
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from pydantic import ValidationError
from sse_starlette import EventSourceResponse
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import Receive, Scope, Send

import mcp.types as types

logger = logging.getLogger(__name__)


class SseServerTransport:
    """
    SSE server transport for MCP. This class provides _two_ ASGI applications,
    suitable to be used with a framework like Starlette and a server like Hypercorn:

        1. connect_sse() is an ASGI application which receives incoming GET requests,
           and sets up a new SSE stream to send server messages to the client.
        2. handle_post_message() is an ASGI application which receives incoming POST
           requests, which should contain client messages that link to a
           previously-established SSE session.
    """

    _endpoint: str
    _read_stream_writers: dict[
        str, MemoryObjectSendStream[types.JSONRPCMessage | Exception]
    ]

    def __init__(self, name: str, transport: str, endpoint: str) -> None:
        """
        Creates a new SSE server transport, which will direct the client to POST
        messages to the relative or absolute URL given.
        """

        super().__init__()
        self._endpoint = endpoint
        self._read_stream_writers = {}
        logger.debug(f"SseServerTransport initialized with endpoint: {endpoint}")

        self._wallet = tsp.SecureStore()
        self._did = self._wallet.resolve_alias(name)

        if self._did is None:
            # Initialize TSP identity
            self._did = (
                f"did:web:did.teaspoon.world:endpoint:tmcp_server-{name}-{uuid4()}"
            )
            identity = tsp.OwnedVid.bind(self._did, transport)

            # Publish DID (this is non-standard and dependents on the implementation of
            # the DID support server)
            response = requests.post(
                "https://did.teaspoon.world/add-vid",
                data=identity.json(),
                headers={"Content-type": "application/json"},
            )
            if not response.ok:
                raise Exception(
                    f"Could not publish DID (status code: {response.status_code}):"
                    f"\n{identity.json()}"
                )
            print("Published server DID: " + self._did)

            self._wallet.add_private_vid(identity, name)

        else:
            print("Using existing DID: " + self._did)

    @asynccontextmanager
    async def connect_sse(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            logger.error("connect_sse received non-HTTP request")
            raise ValueError("connect_sse can only handle HTTP requests")

        logger.debug("Setting up SSE connection")
        read_stream: MemoryObjectReceiveStream[types.JSONRPCMessage | Exception]
        read_stream_writer: MemoryObjectSendStream[types.JSONRPCMessage | Exception]

        write_stream: MemoryObjectSendStream[types.JSONRPCMessage]
        write_stream_reader: MemoryObjectReceiveStream[types.JSONRPCMessage]

        read_stream_writer, read_stream = anyio.create_memory_object_stream(0)
        write_stream, write_stream_reader = anyio.create_memory_object_stream(0)

        request = Request(scope, receive)
        user_did = request.query_params.get("did")
        if user_did is None:
            logger.warning("Received request without user did")
            raise Exception("did is required")
        self._wallet.resolve_did_web(user_did)

        session_uri = quote(self._endpoint)
        logger.debug(f"Created new session with ID: {user_did}")
        self._read_stream_writers[user_did] = read_stream_writer

        sse_stream_writer, sse_stream_reader = anyio.create_memory_object_stream[
            dict[str, Any]
        ](0)

        async def sse_send(event, data):
            self._wallet.resolve_did_web(user_did)
            json_message = json.dumps({"event": event, "data": data}).encode("utf-8")
            logger.info(f"Encoding TSP message: {json_message}")
            _, tsp_message = self._wallet.seal_message(
                self._did, user_did, json_message
            )
            logger.info("Sending TSP message:")
            tsp.color_print(tsp_message)
            encoded_message = base64.b64encode(tsp_message, b"-_").decode()
            await sse_stream_writer.send({"event": "message", "data": encoded_message})

        async def sse_writer():
            logger.debug("Starting SSE writer")
            async with sse_stream_writer, write_stream_reader:
                await sse_send("endpoint", session_uri)
                logger.debug(f"Sent endpoint event: {session_uri}")

                async for message in write_stream_reader:
                    await sse_send(
                        "message",
                        message.model_dump_json(by_alias=True, exclude_none=True),
                    )

        async with anyio.create_task_group() as tg:
            response = EventSourceResponse(
                content=sse_stream_reader, data_sender_callable=sse_writer
            )
            logger.debug("Starting SSE response task")
            tg.start_soon(response, scope, receive, send)

            logger.debug("Yielding read and write streams")
            yield (read_stream, write_stream)

    async def handle_post_message(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        logger.debug("Handling POST message")
        request = Request(scope, receive)

        # Open TSP message (only works for known sender DIDs)
        body = await request.body()
        logger.info("Received TSP message:")
        tsp.color_print(body)
        (sender, receiver) = self._wallet.get_sender_receiver(body)
        if receiver != self._did:
            logger.warning(f"Received message intended for: {receiver}")
            response = Response("Incorrect receiver", status_code=400)
            return await response(scope, receive, send)

        json_text = self._wallet.open_message(body).message
        logger.info(f"Decoded TSP message: {json_text}")

        writer = self._read_stream_writers.get(sender)
        if not writer:
            logger.warning(f"Could not find session for ID: {sender}")
            response = Response("Could not find session", status_code=404)
            return await response(scope, receive, send)

        try:
            message = types.JSONRPCMessage.model_validate_json(json_text)
            logger.debug(f"Validated client message: {message}")
        except ValidationError as err:
            logger.error(f"Failed to parse message: {err}")
            response = Response("Could not parse message", status_code=400)
            await response(scope, receive, send)
            await writer.send(err)
            return

        logger.debug(f"Sending message to writer: {message}")
        response = Response("Accepted", status_code=202)
        await response(scope, receive, send)
        await writer.send(message)
