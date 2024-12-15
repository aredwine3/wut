# Standard library
import os
import re
import tempfile
from collections import namedtuple
from subprocess import check_output, run, CalledProcessError
from typing import List, Optional, Tuple

# Third party
import tiktoken
from psutil import Process
from openai import OpenAI
from anthropic import Anthropic
from rich.markdown import Markdown

# Local
from wut.prompts import EXPLAIN_PROMPT, ANSWER_PROMPT

# from prompts import EXPLAIN_PROMPT, ANSWER_PROMPT

MAX_HISTORY_LINES = 10000
SHELLS = ["bash", "fish", "zsh", "csh", "tcsh", "powershell", "pwsh"]

tokenizer = tiktoken.get_encoding("cl100k_base")

Shell = namedtuple("Shell", ["path", "name", "prompt"])
Command = namedtuple("Command", ["text", "output"])


#########
# HELPERS
#########


def count_tokens(text: str) -> int:
    return len(tokenizer.encode(text, disallowed_special=()))


def truncate_tokens(text: str, max_tokens: int, reverse: bool = False) -> str:
    tokens = tokenizer.encode(text, disallowed_special=())
    tokens = tokens[-max_tokens:] if reverse else tokens[:max_tokens]
    return tokenizer.decode(tokens)


def get_shell_name(shell_path: Optional[str] = None) -> Optional[str]:
    if not shell_path:
        return None

    if os.path.splitext(shell_path)[-1].lower() in SHELLS:
        return os.path.splitext(shell_path)[-1].lower()

    if os.path.splitext(shell_path)[0].lower() in SHELLS:
        return os.path.splitext(shell_path)[0].lower()

    if shell_path.lower() in SHELLS:
        return shell_path.lower()

    return None


def get_shell_name_and_path() -> Tuple[Optional[str], Optional[str]]:
    path = os.environ.get("SHELL", None) or os.environ.get("TF_SHELL", None)
    if shell_name := get_shell_name(path):
        return shell_name, path

    proc = Process(os.getpid())
    while proc is not None and proc.pid > 0:
        try:
            _path = proc.name()
        except TypeError:
            _path = proc.name

        if shell_name := get_shell_name(_path):
            return shell_name, _path

        try:
            proc = proc.parent()
        except TypeError:
            proc = proc.parent

    return None, path


def get_shell_prompt(shell_name: str, shell_path: str) -> Optional[str]:
    shell_prompt = None
    try:
        if shell_name == "zsh":
            cmd = [
                shell_path,
                "-c",
                "print -P $PS1",
            ]
            shell_prompt = check_output(cmd, text=True)
        elif shell_name == "bash":
            # Uses parameter transformation; only supported in Bash 4.4+
            cmd = [
                shell_path,
                "echo",
                '"${PS1@P}"',
            ]
            shell_prompt = check_output(cmd, text=True)
        elif shell_name == "fish":
            cmd = [shell_path, "fish_prompt"]
            shell_prompt = check_output(cmd, text=True)
        elif shell_name in ["csh", "tcsh"]:
            cmd = [shell_path, "-c", "echo $prompt"]
            shell_prompt = check_output(cmd, text=True)
        elif shell_name in ["pwsh", "powershell"]:
            cmd = [shell_path, "-c", "Write-Host $prompt"]
            shell_prompt = check_output(cmd, text=True)
    except:
        pass

    return shell_prompt.strip() if shell_prompt else None


def get_aliases(shell: Shell) -> str:
    return check_output([shell.path, "-ic", "alias"], text=True).strip()


def get_pane_output() -> str:
    output_file = None
    output = ""
    try:
        with tempfile.NamedTemporaryFile(delete=False) as temp_file:
            output_file = temp_file.name

            if os.getenv("TMUX"):  # tmux session
                cmd = [
                    "tmux",
                    "capture-pane",
                    "-p",
                    "-S",
                    # f"-{MAX_HISTORY_LINES}",
                    "-",
                ]
                with open(output_file, "w") as f:
                    run(cmd, stdout=f, text=True)
            elif os.getenv("STY"):  # screen session
                cmd = ["screen", "-X", "hardcopy", "-h", output_file]
                check_output(cmd, text=True)
            else:
                return ""

            with open(output_file, "r") as f:
                output = f.read()
    except CalledProcessError as e:
        pass

    if output_file:
        os.remove(output_file)

    return output


def get_commands(pane_output: str, shell: Shell) -> List[Command]:
    # TODO: Handle edge cases. E.g. if you change the shell prompt in the middle of a session,
    # only the latest prompt will be used to split the pane output into `Command` objects.

    commands = []  # Order: newest to oldest
    buffer = []
    for line in reversed(pane_output.splitlines()):
        if not line.strip():
            continue

        if shell.prompt.lower() in line.lower():
            command_text = line.split(shell.prompt, 1)[1].strip()
            command = Command(command_text, "\n".join(reversed(buffer)).strip())
            commands.append(command)
            buffer = []
            continue

        buffer.append(line)

    return commands[1:]  # Exclude the wut command itself


def truncate_commands(commands: List[Command], max_tokens: int) -> List[Command]:
    num_tokens = 0
    truncated_commands = []
    for command in commands:
        command_tokens = count_tokens(command.text)
        if command_tokens + num_tokens > max_tokens:
            break

        output = []
        for line in reversed(command.output.splitlines()):
            line_tokens = count_tokens(line)
            if line_tokens + num_tokens > max_tokens:
                break

            output.append(line)

        output = "\n".join(reversed(output))
        command = Command(command.text, output)
        truncated_commands.append(command)

    return truncated_commands


def truncate_pane_output(output: str, max_tokens: int) -> str:
    hit_non_empty_line = False
    lines = []  # Order: newest to oldest
    for line in reversed(output.splitlines()):
        if line and line.strip():
            hit_non_empty_line = True

        if hit_non_empty_line:
            lines.append(line)

    lines = lines[1:]  # Remove wut command
    output = "\n".join(reversed(lines))
    output = truncate_tokens(output, max_tokens, reverse=True)
    output = output.strip()

    return output


def command_to_string(command: Command, shell_prompt: Optional[str] = None) -> str:
    shell_prompt = shell_prompt if shell_prompt else "$"
    command_str = f"{shell_prompt} {command.text}"
    command_str += f"\n{command.output}" if command.output.strip() else ""
    return command_str


def format_output(output: str) -> str:
    return Markdown(
        output,
        code_theme="monokai",
        inline_code_lexer="python",
        inline_code_theme="monokai",
    )


def run_anthropic(system_message: str, user_message: str) -> str:
    anthropic = Anthropic()
    response = anthropic.messages.create(
        model="claude-3-5-sonnet-20241022",
        max_tokens=1024,
        system=system_message,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text


def run_openai(system_message: str, user_message: str) -> str:
    openai = OpenAI()
    response = openai.chat.completions.create(
        messages=[
            {"role": "system", "content": system_message},
            {"role": "user", "content": user_message},
        ],
        model="gpt-4o",
        temperature=0.7,
    )
    return response.choices[0].message.content


def get_llm_provider() -> str:
    if os.getenv("OPENAI_API_KEY", None):  # Default
        return "openai"

    if os.getenv("ANTHROPIC_API_KEY", None):
        return "anthropic"

    raise ValueError("No API key found for OpenAI or Anthropic.")


######
# MAIN
######


def get_shell() -> Shell:
    name, path = get_shell_name_and_path()
    prompt = get_shell_prompt(name, path)
    return Shell(path, name, prompt)  # NOTE: Could all be null values


def get_terminal_context(shell: Shell, max_tokens=4096, max_commands=3) -> str:
    pane_output = get_pane_output()
    if not pane_output:
        return "<terminal_history>No terminal output found.</terminal_history>"

    if not shell.prompt:
        # W/o the prompt, we can't reliably separate commands in terminal output
        pane_output = truncate_pane_output(pane_output, max_tokens)
        context = f"<terminal_history>\n{pane_output}\n</terminal_history>"
    else:
        commands = get_commands(pane_output, shell)
        commands = truncate_commands(commands[:max_commands], max_tokens)
        commands = list(reversed(commands))  # Order: Oldest to newest

        previous_commands = commands[:-1]
        last_command = commands[-1]

        context = "<terminal_history>\n"
        context += "<previous_commands>\n"
        context += "\n".join(
            command_to_string(c, shell.prompt) for c in previous_commands
        )
        context += "\n</previous_commands>\n"
        context += "\n<last_command>\n"
        context += command_to_string(last_command, shell.prompt)
        context += "\n</last_command>"
        context += "\n</terminal_history>"

    return context


def get_system_context(shell: Shell) -> str:
    system = check_output(["uname", "-a"], text=True).strip()
    aliases = get_aliases(shell)

    system = f"<system>{system}</system>"
    shell = f"<shell>{shell.name or shell.path}</shell>"
    aliases = f"<aliases>\n{aliases}\n</aliases>"

    return f"<system_info>\n{system}\n{shell}\n{aliases}\n</system_info>"


def build_query(context: str, query: Optional[str] = None) -> str:
    if not (query and query.strip()):
        query = "Explain the last command's output. Use the previous commands as context, if relevant, but focus on the last command."

    return f"{context}\n\n{query}"


def explain(context: str, query: Optional[str] = None) -> str:
    system_message = EXPLAIN_PROMPT if not query else ANSWER_PROMPT
    user_message = build_query(context, query)
    provider = get_llm_provider()
    call_llm = run_openai if provider == "openai" else run_anthropic
    output = call_llm(system_message, user_message)
    return format_output(output)