#!/usr/bin/env python3

import asyncio
import argparse
import logging

from meshcore import MeshCore, EventType

logger = logging.getLogger(__name__)

# See the IRC protocol RFC for details
# https://www.rfc-editor.org/rfc/rfc1459

class Bridge:
    def __init__(self, meshcore):
        self.mesh = meshcore
        self.client = None

    async def start(self, host, port):
        await self.mesh.ensure_contacts()
        self.mesh.subscribe(EventType.CHANNEL_MSG_RECV, self._on_mesh_channel_msg)
        self.mesh.subscribe(EventType.CONTACT_MSG_RECV, self._on_mesh_private_msg)
        await self.mesh.start_auto_message_fetching()

        server = await asyncio.start_server(self._handle_client, host, port)

        async with server:
            await server.serve_forever()

    async def _on_mesh_channel_msg(self, event):
        logging.debug(event)

        if not self.client:
            return

        payload = event.payload

        if payload.get("type") != "CHAN":
            return

        if payload.get("channel_idx") != 0:
            return

        if payload.get("txt_type") != 0:
            return

        message = payload.get("text")

        if not message:
            return

        parts = message.split(":", 1)

        if len(parts) != 2:
            return

        # need better way to get contact name for a channel
        # message; protocol does not even contain pub key
        #nick = parts[0].strip()
        #message = parts[1].strip()
        nick = "mesh"
        message = parts[0] + ":" + parts[1]

        # need better sanitization
        message = message.replace("\r", "")
        message = message.replace("\n", " ")
        message = message.replace("\0", "")

        await self.client.send(f":{nick}!{nick}@mesh PRIVMSG #public :{message}")

    async def _on_mesh_private_msg(self, event):
        logging.debug(event)

        if not self.client:
            return

        payload = event.payload

        if payload.get("type") != "PRIV":
            return

        if payload.get("txt_type") != 0:
            return

        pubkey = payload.get("pubkey_prefix")
        text = payload.get("text")

        if not pubkey or not text:
            return

        # need better way to get contact name for a private
        # message; load contacts and cache and from adverts
        nick = f"{pubkey[:12]}"
        message = text.strip()

        # need better sanitization
        message = message.replace("\r", "")
        message = message.replace("\n", " ")
        message = message.replace("\0", "")

        await self.client.send(f":{nick}!{nick}@mesh PRIVMSG {self.client.nick} :{message}")

    async def _handle_client(self, reader, writer):
        logging.debug("IRC client connected")

        client = Client(reader, writer)

        if not self.client:
            self.client = client
        else:
            logging.error("IRC client already connected")
            await client.close()
            return

        try:
            async for line in reader:
                msg = line.decode("utf-8", errors="ignore").strip()

                if msg:
                    await self._handle_irc_msg(client, msg)
        except ConnectionResetError:
            pass
        except Exception as e:
            logging.exception(e)
        finally:
            await client.close()
            self.client = None

        logging.debug("IRC client disconnected")

    async def _handle_irc_msg(self, client, msg):
        logging.debug(f" < {msg}")

        parts = msg.split(" ", 1)
        cmd = parts[0].upper()
        params = parts[1] if len(parts) > 1 else ""

        match cmd:
            case "NICK":
                await self._handle_nick(client, params)
            case "USER":
                await self._handle_user(client, params)
            case "QUIT":
                await self._handle_quit(client, params)
            case "JOIN":
                await self._handle_join(client, params)
            case "PART":
                await self._handle_part(client, params)
            case "MODE":
                await self._handle_mode(client, params)
            case "LIST":
                await self._handle_list(client, params)
            case "PRIVMSG":
                await self._handle_privmsg(client, params)
            case "WHOIS":
                await self._handle_whois(client, params)
            case "WHO":
                await self._handle_who(client, params)
            case "PING":
                await self._handle_ping(client, params)
            case _:
                pass

    async def _handle_nick(self, client, params):
        if not params:
            await client.send(f":mesh 431 {client.nick} :No nickname given")
            return

        nick = params.split(" ")[0]

        if nick.startswith("#"):
            await client.send(f":mesh 432 {client.nick} {nick} :Erroneus nickname")
            return

        client.nick = nick

    async def _handle_user(self, client, params):
        if client.user:
            await client.send(f":mesh 462 {client.nick} :You may not reregister")
            return

        if not params:
            await client.send(f":mesh 461 {client.nick} USER :Not enough parameters")
            return

        client.user = params.split(" ")[0]

        contacts = self.mesh.contacts.keys()

        await client.send(f":mesh 001 {client.nick} :Welcome to MeshCore")
        await client.send(f":mesh 002 {client.nick} :{len(contacts)} contacts")
        await client.send(f":mesh 376 {client.nick} :End MOTD")

    async def _handle_quit(self, client, params):
        await client.send(f":{client.prefix} QUIT :{client.nick}")

    async def _handle_join(self, client, params):
        if not params:
            await client.send(f":mesh 461 {client.nick} JOIN :Not enough parameters")
            return

        parts = params.split(" ", 1)
        channels = parts[0]

        for chan in channels.split(","):
            if chan == "#public":
                client.channels.add(chan)

                await client.send(f":{client.prefix} JOIN #public")
                await client.send(f":mesh 332 {client.nick} #public :MeshCore Public Channel")
                await client.send(f":mesh 353 {client.nick} = #public :{client.nick}")
                await client.send(f":mesh 366 {client.nick} #public :End of /NAMES list")
            else:
                await client.send(f":mesh 403 {client.nick} {chan} :No such channel")

    async def _handle_part(self, client, params):
        if not params:
            await client.send(f":mesh 461 {client.nick} PART :Not enough parameters")
            return

        parts = params.split(" ", 1)
        channels = parts[0]

        for chan in channels.split(","):
            if chan not in client.channels:
                await client.send(f":mesh 442 {client.nick} {chan} :You're not on that channel")
                continue

            await client.send(f":{client.prefix} PART {chan}")
            client.channels.remove(chan)

    async def _handle_mode(self, client, params):
        if not params:
            await client.send(f":mesh 461 {client.nick} MODE :Not enough parameters")
            return

        parts = params.split(" ")
        target = parts[0]

        if target.startswith("#"):
            if target not in client.channels:
                await client.send(f":mesh 442 {client.nick} {target} :You're not on that channel")
                return

            if target == "#public":
                await client.send(f":mesh 324 {client.nick} #public +nt")
        else:
            await client.send(f":mesh 502 {client.nick} :Cant change mode for other users")

    async def _handle_list(self, client, params):
        if not params:
            await client.send(f":mesh 321 {client.nick} Channel :Users Name")
            await client.send(f":mesh 322 {client.nick} #public 0 :Public")
            await client.send(f":mesh 323 {client.nick} :End of LIST")

            return

        for chan in params.split(","):
            if chan == "#public":
                await client.send(f":mesh 321 {client.nick} Channel :Users Name")
                await client.send(f":mesh 322 {client.nick} #public 0 :Public")
                await client.send(f":mesh 323 {client.nick} :End of LIST")
            else:
                await client.send(f":mesh 403 {client.nick} {chan} :No such channel")

    async def _handle_privmsg(self, client, params):
        target, _, msg = params.partition(" ")
        msg = msg.lstrip(":")

        if not msg:
            await client.send(f":mesh 412 {client.nick} :No text to send")
            return

        if target.startswith("#"):
            if target == "#public":
                result = await self.mesh.commands.send_chan_msg(0, msg)

                if result.type == EventType.ERROR:
                    await client.send(f":mesh NOTICE {client.nick} :Failed to send message to {target}")
            else:
                await client.send(f":mesh 401 {client.nick} {target} :No such nick/channel")

        elif target == client.nick:
            await client.send(f":{client.prefix} PRIVMSG {client.nick} :{msg}")

        else:
            contact = self.mesh.get_contact_by_key_prefix(target)

            if contact:
                result = await self.mesh.commands.send_msg_with_retry(contact, msg)

                if not result:
                    await client.send(f":mesh NOTICE {client.nick} :Failed to send message to {target}")
            else:
                await client.send(f":mesh 401 {client.nick} {target} :No such nick/channel")

    async def _handle_whois(self, client, params):
        parts = params.split(" ")

        if not parts:
            await client.send(f":mesh 431 {client.nick} :No nickname given")
            return

        target = parts[-1]

        if target == client.nick:
            await client.send(f":mesh 311 {client.nick} {client.nick} {client.user} mesh * :{client.nick}")
            await client.send(f":mesh 312 {client.nick} {client.nick} mesh :MeshCore IRC Bridge")

            if client.channels:
                channels = " ".join(sorted(client.channels))
                await client.send(f":mesh 319 {client.nick} {client.nick} :{channels}")

            await client.send(f":mesh 318 {client.nick} {client.nick} :End of /WHOIS list")
        else:
            await client.send(f":mesh 401 {client.nick} {target} :No such nick/channel")
            await client.send(f":mesh 318 {client.nick} {target} :End of /WHOIS list")

    async def _handle_who(self, client, params):
        parts = params.split(" ")
        target = parts[0] if parts else "*"

        if target in client.channels:
            await client.send(f":mesh 352 {client.nick} {target} {client.user} mesh mesh {client.nick} H :0 {client.nick}")
        elif target == "*" or target == "0":
            for chan in sorted(client.channels):
                await client.send(f":mesh 352 {client.nick} {chan} {client.user} mesh mesh {client.nick} H :0 {client.nick}")
        elif target == client.nick:
            for chan in sorted(client.channels):
                await client.send(f":mesh 352 {client.nick} {chan} {client.user} mesh mesh {client.nick} H :0 {client.nick}")

        await client.send(f":mesh 315 {client.nick} {target} :End of /WHO list")

    async def _handle_ping(self, client, params):
        await client.send(f":mesh PONG mesh :{params}")


class Client:
    def __init__(self, reader, writer):
        self.reader = reader
        self.writer = writer
        self.nick = "*"
        self.user = None
        self.channels = set()

    async def send(self, msg):
        logging.debug(f" > {msg}")
        self.writer.write(f"{msg}\r\n".encode())
        await self.writer.drain()

    async def close(self):
        try:
            self.writer.close()
            await self.writer.wait_closed()
        except ConnectionResetError:
            pass

    @property
    def prefix(self):
        return f"{self.nick}!{self.user}@mesh"


async def main():
    parser = argparse.ArgumentParser(description="MeshCore IRC bridge")

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--serial", help="MeshCore device serial port")
    group.add_argument("--ble", help="MeshCore device BLE address")

    parser.add_argument("--port", type=int, default=6667, help="IRC server port")
    parser.add_argument("--host", default="127.0.0.1", help="IRC server host")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug mode")

    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=level, format="%(message)s")

    if args.serial:
        mc = await MeshCore.create_serial(args.serial)
    elif args.ble:
        mc = await MeshCore.create_ble(args.ble)

    bridge = Bridge(mc)

    try:
        await bridge.start(args.host, args.port)
    finally:
        await mc.stop_auto_message_fetching()
        mc.stop()
        await mc.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
