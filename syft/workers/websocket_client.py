import binascii
from typing import Union
from typing import List

import torch
import websocket
import websockets
import logging
import ssl
import time

import syft as sy
from syft.codes import MSGTYPE
from syft.frameworks.torch.tensors.interpreters import AbstractTensor
from syft.workers import BaseWorker

logger = logging.getLogger(__name__)


TIMEOUT_INTERVAL = 9_999_999


class WebsocketClientWorker(BaseWorker):
    def __init__(
        self,
        hook,
        host: str,
        port: int,
        secure: bool = False,
        id: Union[int, str] = 0,
        is_client_worker: bool = False,
        log_msgs: bool = False,
        verbose: bool = False,
        data: List[Union[torch.Tensor, AbstractTensor]] = None,
    ):
        """A client which will forward all messages to a remote worker running a
        WebsocketServerWorker and receive all responses back from the server.
        """

        # TODO get angry when we have no connection params
        self.port = port
        self.host = host

        super().__init__(hook, id, data, is_client_worker, log_msgs, verbose)

        # creates the connection with the server which gets held open until the
        # WebsocketClientWorker is garbage collected.
        # Secure flag adds a secure layer applying cryptography and authentication
        self.uri = ""
        self.secure = secure
        self.ws = None
        self.connect()

    def connect(self):
        args = dict()
        args["url"] = f"ws://{self.host}:{self.port}"
        args["max_size"] = None
        args["timeout"] = TIMEOUT_INTERVAL
        if self.secure:
            args["uri"] = f"wss://{self.host}:{self.port}"
            args["sslopt"] = {"cert_reqs": ssl.CERT_NONE}
        self.uri = args["url"]
        self.ws = websocket.create_connection(**args)

    def close(self):
        self.ws.shutdown()

    def search(self, *query):
        # Prepare a message requesting the websocket server to search among its objects
        message = (MSGTYPE.SEARCH, query)
        serialized_message = sy.serde.serialize(message)
        # Send the message and return the deserialized response.
        response = self._recv_msg(serialized_message)
        return sy.serde.deserialize(response)

    def _send_msg(self, message: bin, location) -> bin:
        raise RuntimeError(
            "_send_msg should never get called on a ",
            "WebsocketClientWorker. Did you accidentally "
            "make hook.local_worker a WebsocketClientWorker?",
        )

    def _receive_action(self, message: bin) -> bin:
        self.ws.send(str(binascii.hexlify(message)))
        response = binascii.unhexlify(self.ws.recv()[2:-1])
        return response

    def _recv_msg(self, message: bin) -> bin:
        """Forwards a message to the WebsocketServerWorker"""

        response = self._receive_action(message)
        if not self.ws.connected:
            logger.warning("Websocket connection closed (worker: %s)", self.id)
            self.ws.shutdown()
            time.sleep(0.1)
            # Avoid timing out on the server-side
            self.ws = websocket.create_connection(self.uri, max_size=None, timeout=TIMEOUT_INTERVAL)
            logger.warning("Created new websocket connection")
            time.sleep(0.1)
            response = self._receive_action(message)
            if not self.ws.connected:
                raise RuntimeError(
                    "Websocket connection closed and creation of new connection failed."
                )
        return response

    def _send_msg_and_deserialize(self, command_name: str, *args, **kwargs):
        message = self.create_message_execute_command(
            command_name=command_name, command_owner="self", *args, **kwargs
        )

        # Send the message and return the deserialized response.
        serialized_message = sy.serde.serialize(message)
        response = self._recv_msg(serialized_message)
        return sy.serde.deserialize(response)

    def list_objects_remote(self):
        return self._send_msg_and_deserialize("list_objects")

    def objects_count_remote(self):
        return self._send_msg_and_deserialize("objects_count")

    async def fit(self, dataset_key, **kwargs):
        # Arguments provided as kwargs as otherwise miss-match
        # with signature in FederatedClient.fit()
        return_ids = kwargs["return_ids"] if "return_ids" in kwargs else [sy.ID_PROVIDER.pop()]

        self.close()
        url = f"ws://{self.host}:{self.port}"
        async with websockets.connect(
            url, timeout=TIMEOUT_INTERVAL, max_size=None, ping_timeout=TIMEOUT_INTERVAL
        ) as websocket:
            message = self.create_message_execute_command(
                command_name="fit",
                command_owner="self",
                return_ids=return_ids,
                dataset_key=dataset_key,
            )

            # Send the message and return the deserialized response.
            serialized_message = sy.serde.serialize(message)
            await websocket.send(str(binascii.hexlify(serialized_message)))
            await websocket.recv()  # returned value will be None, so don't care
        self.connect()
        msg = (MSGTYPE.OBJ_REQ, return_ids[0])
        # Send the message and return the deserialized response.
        serialized_message = sy.serde.serialize(msg)
        response = self._recv_msg(serialized_message)
        return sy.serde.deserialize(response)

    def synchronous_fit(self, dataset_key, **kwargs):
        # Arguments provided as kwargs as otherwise miss-match
        # with signature in FederatedClient.fit()
        return_ids = kwargs["return_ids"] if "return_ids" in kwargs else [sy.ID_PROVIDER.pop()]

        self._send_msg_and_deserialize("fit", return_ids=return_ids, dataset_key=dataset_key)

        msg = (MSGTYPE.OBJ_REQ, return_ids[0])
        # Send the message and return the deserialized response.
        serialized_message = sy.serde.serialize(msg)
        response = self._recv_msg(serialized_message)
        return sy.serde.deserialize(response)

    def __str__(self):
        """Returns the string representation of a Websocket worker.

        A to-string method for websocket workers that includes information from the websocket server

        Returns:
            The Type and ID of the worker

        """
        out = "<"
        out += str(type(self)).split("'")[1].split(".")[-1]
        out += " id:" + str(self.id)
        out += " #objects local:" + str(len(self._objects))
        out += " #objects remote: " + str(self.objects_count_remote())
        out += ">"
        return out
