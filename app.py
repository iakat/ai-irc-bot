import asyncio
import json
import re
import traceback
from collections import defaultdict
from contextlib import suppress
from os import environ, getenv
from sys import exit
from uuid import uuid4

import aiohttp
from ircrobots import Bot as BaseBot
from ircrobots import ConnectionParams, SASLUserPass
from ircrobots import Server as BaseServer
from ircrobots.security import TLSNoVerify
from irctokens import Line, build

### SETTINGS

IRC_SERVERS = getenv("SUPER_IRC_SERVERS", "libera,irc.libera.chat,6697").split(";")
for i, server in enumerate(IRC_SERVERS):
    name, host, port = server.split(",")
    port = int(port)
    IRC_SERVERS[i] = (name, host, port)
IRC_NICK = getenv("SUPER_IRC_NICK", "Super")
IRC_USERNAME = getenv("SUPER_IRC_USERNAME", getenv("SUPER_IRC_NICK", "Super"))
IRC_PASSWORD = getenv("SUPER_IRC_PASSWORD", "")
IRC_CHANNELS = getenv("SUPER_IRC_CHANNELS").split(",")

MODEL = getenv("SUPER_MODEL", "mixtral-8x22b")

ALLOWED_URLS = getenv(
    "SUPER_ALLOWED_URLS", "https://i.katia.sh/,https://paste.ee/r/"
).split(",")

FORGIVE_ME = [set(), set()]
INFERENCE_URL = "http://gpt4free.gpt4free/backend-api/v2/conversation"


#### CONVERSATION
class Conversation:
    def __init__(self, _persist=False, _prompt=None):
        self.semaphore = asyncio.Semaphore(1)
        self.client = aiohttp.ClientSession()

        self.uuid = uuid4()
        self._persist_prompt = _persist
        if _prompt:
            self._messages = list({"role": "system", "content": _prompt})
        self._stopped = False
        self._allow_args = (  # thing, min, max
            ("temperature", 0.0, 1.0),
            ("presence_penalty", -2.0, 2.0),
            ("frequency_penalty", -2.0, 2.0),
            ("repetition_penalty", 0.01, 5.0),
            ("top_k", 0.0, 100000.0),
            ("top_p", 0.0, 1.0),
            ("max_tokens", 1.0, 10240.0),
        )
        self._args = {
            "temperature": 0.85,
            "presence_penalty": 1.1,
            "frequency_penalty": 1.3,
            "repetition_penalty": 1.2,
        }

    # on class destruction, close the client
    def __del__(self):
        asyncio.create_task(self.client.close())

    async def _initial_prompt(self):
        # synchronously _get_url
        with suppress(Exception):
            res = await self._get_url(f"https://i.katia.sh/ai-irc-bots/{IRC_NICK}.txt")
            if not res or res.status == 404 or res == "Not Found":
                raise Exception("no prompt")
            return [
                {
                    "role": "system",
                    "content": "Hello! How can I assist you today?",
                }
            ]
        return []

    def remove_own_nick(self, line):
        # case insensitive
        line = re.sub(rf"^{IRC_NICK}[:,]?", "", line, flags=re.IGNORECASE)
        line = re.sub(rf"^\<{IRC_NICK}\>", "", line, flags=re.IGNORECASE)
        return line

    def sanitise(self, line):
        line = self.remove_own_nick(line).rstrip()
        # remove invalid unicode, not isprintable, just invalid
        line = "".join(char for char in line if char.isprintable())
        return line

    async def _speak_gpt4free(self, message):
        res, res_full, web = "", "", False
        self._stopped = False

        # if y2wb is in message, we say YES to Web Search
        if "y2wb" in message:
            message, web = message.replace("y2wb", ""), True

        # normalise the number of spaces in the message
        message = re.sub(r"\s+", " ", message)

        self._messages.append({"role": "user", "content": message})

        jsondata = {
            "messages": self._messages,
            "model": MODEL,
            "conversation_id": self.uuid.hex,
            "id": int(self.uuid) << 64,
            "web_search": web,
            **self._args,
        }
        # max_tokens oughta be an int, if its there.
        if "max_tokens" in jsondata:
            jsondata["max_tokens"] = int(jsondata["max_tokens"])
        for key, value in jsondata.items():
            print(f"jsondata.{key}={value}")
        async with self.client.post(INFERENCE_URL, json=jsondata) as response:
            async for line in response.content:
                j = json.loads(line)
                print(line)
                if j["type"] != "content":
                    # we do not care about non-content
                    continue
                if j["content"].startswith("<g4f."):
                    # ... something from upstream, ignore
                    continue
                # otherwise, let's first put it in res_full
                res_full += j["content"]
                # then, we check if we would add a \n here, if so, do not add it, and yield instead
                if "\n" in j["content"]:
                    # yield what came before the \n
                    split = j["content"].split("\n")
                    yield res + split[0]
                    res = "".join(split[1:])
                    continue
                # if we are longer than 420 chars and end with space, or longer than 440, yield
                if (
                    len(res) >= 350 and (res.endswith(" ") or res.endswith("."))
                ) or len(res) > 400:
                    yield res
                    res = j["content"]
                    continue
                # otherwise, add it to res
                res += j["content"]
        if res:
            yield res
        self._messages.append({"role": "assistant", "content": res_full})

    async def _get_url(self, url):
        if not any(url.startswith(allowed) for allowed in ALLOWED_URLS):
            return False
        timeout = aiohttp.ClientTimeout(total=3)
        async with self.client.get(url, timeout=timeout) as response:
            if response.status == 404:
                raise Exception("404")
            res = await response.text()
            # if 404, raise an exception
        return res

    async def speak(self, nickname, message):
        async with self.semaphore:
            message = f"<{nickname}> {message}"
            should_pass = True
            # if _IP= in the message, reset and use that as initial prompt
            if len(_ip := message.split(" .sip ")) > 1:
                if _ip[1] == "reset":
                    self._messages = await self._initial_prompt()
                    yield "resetting prompt..."
                    should_pass = False
                    return
                if not (prompt := await self._get_url(_ip[1])):
                    yield "I cannot access that URL."
                    return
                self._persist_prompt = True
                yield "prompt set to:" + prompt[:80] + "..."
                self._messages = [{"role": "system", "content": prompt}]
                should_pass = False
            elif len(_q := message.split(" .q ")) > 1:
                yield f"querying just {_q[1]}"
                message = _q[1]
            elif len(message.split(" .pop")) > 1:
                if self._messages:
                    if len(self._messages) >= 3:
                        self._messages.pop()
                        self._messages.pop()
                        yield "removed last interaction from my memory..."
                    else:
                        yield "wat? " + self._messages[0]["content"]
                should_pass = False
            elif len(message.split(" .help")) > 1:
                yield ".reset - forget everything | .sip <https://paste.ee/r/...> - set initial prompt & reset"
                yield ".q - infer only on following text  | .pop - remove last interaction | .stop - try to stop me"
                yield "--temperature, --presence_penalty, --repetition_penalty, --top_k, --top_p, --max_tokens"
                should_pass = False
            # let's handle --extra-args if should_pass is still True by now
            if should_pass:
                for arg, min_v, max_v in self._allow_args:
                    if len(_arg := message.split(f" --{arg} ")) > 1:
                        should_pass = False
                        try:
                            candidate = float(_arg[1].split()[0])
                            if not min_v <= candidate <= max_v:
                                raise ValueError
                        except:
                            current = self._args[arg] if arg in self._args else None
                            yield f"um? {arg} is currently {current}. min={min_v}, max={max_v}"
                            continue
                        self._args[arg] = candidate
                        yield f"set {arg} to {candidate}"
            print(f"message={message}")
            if not should_pass:
                return
            async for message in self._speak_gpt4free(
                message,
            ):
                if self._stopped:
                    return
                yield self.sanitise(message)


###
CONVERSATIONS = defaultdict(Conversation)


class Server(BaseServer):
    async def line_read(self, line: Line):
        print(f"{self.name} < {line.format()}")
        if line.command == "001":
            print(f"connected to {self.isupport.network}")
            # join chamnnels
            for channel in IRC_CHANNELS:
                await self.send(build("JOIN", [channel]))
        # if line is a privmsg to us, respond
        try:
            if line.hostmask.nickname == self.nickname:
                return
        except:
            print("what?", line)
            return

        if line.command == "PRIVMSG" and line.params[0][0] == "#":
            # asynchronously call the handle_privmsg function
            asyncio.create_task(self.handle_privmsg(line))

    async def handle_privmsg(self, line: Line):
        message = " ".join(line.params[1:])
        print(f"message={message}")
        # check with a regex because, yolo...
        # either spaces or nothing or punctuation need to surround the nick
        # so we dont get alerted for supergirl
        nick_match = re.search(rf"(\b{self.nickname}\b)", message)
        is_reset = message.startswith(".reset") and (nick_match or len(message) == 6)
        is_stop = message.startswith(".stop") and nick_match
        is_katia = line.hostmask.nickname == "katia"
        if "--forgive-me" in message:
            # increase forgive-me,
            # if 3, send QUIT and exit
            FORGIVE_ME[0].add(line.hostmask.hostname)
            FORGIVE_ME[1].add(line.hostmask.nickname)
            forgiven = " ".join(FORGIVE_ME[1])
            if len(FORGIVE_ME[0]) >= 3:
                await self.send(
                    build("PRIVMSG", [line.params[0], f"i forgive you {forgiven} :'("])
                )
                await self.send(build("QUIT"))
                exit(0)
            else:
                await self.send(
                    build("PRIVMSG", [line.params[0], f"i forgive you {forgiven}"])
                )
                return

        if nick_match and is_katia and ".s-m" in message:
            global MODEL
            MODEL = message.split(".s-m ")[1]
            self.send(build("PRIVMSG", [line.params[0], f"set model to {MODEL}"]))
            return
        if is_stop:
            CONVERSATIONS[line.params[0]]._stopped = True
            self.send(build("PRIVMSG", [line.params[0], ":<"]))
            return
        if is_reset:
            if not hasattr(CONVERSATIONS[line.params[0]], "_messages"):
                return  # early exit, no need to reset
            if len(CONVERSATIONS[line.params[0]]._messages) < 2:
                return  # early exit, no need to reset
            with suppress(KeyError):
                oldCon, newCon = CONVERSATIONS[line.params[0]], Conversation()
                oldCon._stopped = True

                if oldCon._persist_prompt:
                    newCon._persist_prompt = oldCon._persist_prompt
                    newCon._messages = list([oldCon._messages[0]])
                CONVERSATIONS[line.params[0]] = newCon
                del oldCon

            self.send(build("PRIVMSG", [line.params[0], "i forgot everything"]))
            return
        if not hasattr(CONVERSATIONS[line.params[0]], "_messages"):
            CONVERSATIONS[line.params[0]]._messages = await CONVERSATIONS[
                line.params[0]
            ]._initial_prompt()

        if not nick_match:
            return
        async for res_line in CONVERSATIONS[line.params[0]].speak(
            line.hostmask.nickname,
            message.strip(),
        ):
            if not res_line or CONVERSATIONS[line.params[0]]._stopped:
                continue
            await self.send(
                build("PRIVMSG", [line.params[0], self.irc_format_line(res_line)])
            )

    async def line_send(self, line: Line):
        print(f"{self.name} > {line.format()}")

    def irc_format_line(self, line: str):
        BOLD_CODE = "\x02"
        line = re.sub(r"[\*]{2,3}(.*?)[\*]{2,3}", rf"{BOLD_CODE}\1{BOLD_CODE}", line)
        return line


class Bot(BaseBot):
    def create_server(self, name: str):
        return Server(self, name)


async def main():
    bot = Bot()
    for name, host, port in IRC_SERVERS:
        print(f"adding server {name} {host}:{port}")
        await bot.add_server(
            name,
            ConnectionParams(
                IRC_NICK,
                host=host,
                port=port,
                # sasl=SASLUserPass(IRC_USERNAME, IRC_PASSWORD),
                tls=TLSNoVerify(),
            ),
        )

    await bot.run()


if __name__ == "__main__":
    asyncio.run(main())
