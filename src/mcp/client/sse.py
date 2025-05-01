import base64
import json
import logging
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse
from uuid import uuid4

import anyio
import httpx
import requests
import tsp_python as tsp
from anyio.abc import TaskStatus
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from httpx_sse import ServerSentEvent, aconnect_sse

import mcp.types as types

logger = logging.getLogger(__name__)


def add_request_params(url: str, params: dict) -> str:
    url = urlparse(url)
    query = dict(parse_qsl(url.query))
    query.update(params)
    url = url._replace(query=urlencode(query))
    return urlunparse(url)


def remove_request_params(url: str) -> str:
    return urljoin(url, urlparse(url).path)


@asynccontextmanager
async def sse_client(
    name: str,
    server_did: str,
    headers: dict[str, Any] | None = None,
    timeout: float = 5,
    sse_read_timeout: float = 60 * 5,
):
    """
    Client transport for SSE.

    `sse_read_timeout` determines how long (in seconds) the client will wait for a new
    event before disconnecting. All other HTTP operations are controlled by `timeout`.
    """
    read_stream: MemoryObjectReceiveStream[types.JSONRPCMessage | Exception]
    read_stream_writer: MemoryObjectSendStream[types.JSONRPCMessage | Exception]

    write_stream: MemoryObjectSendStream[types.JSONRPCMessage]
    write_stream_reader: MemoryObjectReceiveStream[types.JSONRPCMessage]

    read_stream_writer, read_stream = anyio.create_memory_object_stream(0)
    write_stream, write_stream_reader = anyio.create_memory_object_stream(0)

    wallet = tsp.SecureStore()
    did = wallet.resolve_alias(name)

    if did is None:
        # Initialize TSP identity
        did = f"did:web:did.teaspoon.world:endpoint:tmcp_client-{name}-{uuid4()}"
        identity = tsp.OwnedVid.bind(did, "tmcpclient://")

        # Publish DID (this is non-standard and dependents on the implementation of the DID support server)
        response = requests.post(
            "https://did.teaspoon.world/add-vid",
            data=identity.json(),
            headers={"Content-type": "application/json"},
        )
        if not response.ok:
            raise Exception(
                f"Could not publish DID (status code: {response.status_code})"
            )
        print("Published client DID:", did)

        wallet.add_private_vid(identity, name)

    else:
        print("Using existing DID: " + did)

    # Resolve server
    url = wallet.resolve_did_web(server_did)
    print("Server endpoint:", url)
    url = add_request_params(url, {"did": did})

    async with anyio.create_task_group() as tg:
        try:
            logger.info(f"Connecting to SSE endpoint: {remove_request_params(url)}")
            async with httpx.AsyncClient(headers=headers) as client:
                async with aconnect_sse(
                    client,
                    "GET",
                    url,
                    timeout=httpx.Timeout(timeout, read=sse_read_timeout),
                ) as event_source:
                    event_source.response.raise_for_status()
                    logger.debug("SSE connection established")

                    async def sse_reader(
                        task_status: TaskStatus[str] = anyio.TASK_STATUS_IGNORED,
                    ):
                        try:
                            async for sse in event_source.aiter_sse():
                                # Open TSP message
                                tsp_message = base64.b64decode(sse.data, "-_")
                                json_message = wallet.open_message(tsp_message).message
                                json_data = json.loads(json_message)
                                sse = ServerSentEvent(**json_data)

                                logger.debug(f"Received SSE event: {sse.event}")
                                match sse.event:
                                    case "endpoint":
                                        endpoint_url = urljoin(url, sse.data)
                                        logger.info(
                                            f"Received endpoint URL: {endpoint_url}"
                                        )

                                        url_parsed = urlparse(url)
                                        endpoint_parsed = urlparse(endpoint_url)
                                        if (
                                            url_parsed.netloc != endpoint_parsed.netloc
                                            or url_parsed.scheme
                                            != endpoint_parsed.scheme
                                        ):
                                            error_msg = (
                                                "Endpoint origin does not match "
                                                f"connection origin: {endpoint_url}"
                                            )
                                            logger.error(error_msg)
                                            raise ValueError(error_msg)

                                        task_status.started(endpoint_url)

                                    case "message":
                                        try:
                                            message = types.JSONRPCMessage.model_validate_json(  # noqa: E501
                                                sse.data
                                            )
                                            logger.debug(
                                                f"Received server message: {message}"
                                            )
                                        except Exception as exc:
                                            logger.error(
                                                f"Error parsing server message: {exc}"
                                            )
                                            await read_stream_writer.send(exc)
                                            continue

                                        await read_stream_writer.send(message)
                                    case _:
                                        logger.warning(
                                            f"Unknown SSE event: {sse.event}"
                                        )
                        except Exception as exc:
                            logger.error(f"Error in sse_reader: {exc}")
                            await read_stream_writer.send(exc)
                        finally:
                            await read_stream_writer.aclose()

                    async def post_writer(endpoint_url: str):
                        try:
                            async with write_stream_reader:
                                async for message in write_stream_reader:
                                    # Encrypt & sign message with TSP
                                    logger.debug(f"Sending client message: {message}")
                                    json_message = json.dumps(
                                        message.model_dump(
                                            by_alias=True,
                                            mode="json",
                                            exclude_none=True,
                                        )
                                    ).encode("utf-8")
                                    _, tsp_message = wallet.seal_message(
                                        did, server_did, json_message
                                    )
                                    response = await client.post(
                                        endpoint_url, data=tsp_message
                                    )
                                    response.raise_for_status()
                                    logger.debug(
                                        "Client message sent successfully: "
                                        f"{response.status_code}"
                                    )
                        except Exception as exc:
                            logger.error(f"Error in post_writer: {exc}")
                        finally:
                            await write_stream.aclose()

                    endpoint_url = await tg.start(sse_reader)
                    logger.info(
                        f"Starting post writer with endpoint URL: {endpoint_url}"
                    )
                    tg.start_soon(post_writer, endpoint_url)

                    try:
                        yield read_stream, write_stream
                    finally:
                        tg.cancel_scope.cancel()
        finally:
            await read_stream_writer.aclose()
            await write_stream.aclose()
