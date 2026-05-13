# Ark

"The Home of the Autobots on Earth"

## Overview
We're going to build our own agentic harness named "Ark" - inspired by OpenClaw, Claude Code, and NotebookLM. The goal is to provide a configurable server process that runs on a UNIX machine and provides one or more LLM-powered agents that have the ability to perform tasks. The agents are activated in two ways:

- Conversational context: users can have one or more sessions with an agent. During the conversation, the agent can naturally invoke tools to achieve its aims.

- Timer events: the agent is woken up, a new session will be created, and the agent will be given a starting prompt it acts on. During the session, the agent can indicate that "updates" should be posted to another session (that the agent owns). These timer events happen either as a result of:
    - Heartbeats (the agent is awoken ever X minutes)
    - Cron events: the agent is awoken at exactly a specific time. Follows UNIX cron job logic exactly.

Agents can manage their own heartbeat and cron schedule.

## Relay Harness Configuration

### User
- Auth token - used by the Harness API

### Agents
Each agent has the following:
- Session Context: Every agent has a session_context.md file which is provided to them at the start of every session.
- API endpoint: the endpoint for the LLM
- Provider: used to determine how to interact with the endpoint
- API key: the API key used to call the LLM
- workspace: the working directory that the LLM primarily writes files to.

### Tools
There can be an entry for each tool specifying custom configurations including:
- API Key (for those requiring an API key)

## Agent Tools

### Read File
The agent can read any file in the entire operating system.

### Write File
The agent can write any file in the operating system.

### List/Find Files
The agent can list any file in the operating system.

### Run Command
The agent can run any command via bash.

### Search Web
This web search tool provides the ability to run web searches via the Brave API.

## Agent Workspace Structure
The workspace has a couple core files:
- `session_context.md` - the session context
- `heartbeat_prompt.md` - the prompt provided to start the heartbeat

File operations:
- The agent is able to read any file in its workspace
- The agent can write any file and create any directories except for the session context.

## Server API

### Authentication
Authentication is handled via a secret, set in the config file.

### Agents

- List agents: this should return the available agents, their status.
- Get Agent Details: this should return a more detailed breakdown about an agent including its underlying LLM, its status, the current cron jobs scheduled, the heartbeat.
- Agent Heartbeat Update
- Agent Cron Update
- Sessions (for a given agent)
  - List session
  - Create session
  - Delete a session
  - Session history: load all the past conversational elements of a session
  - Session interaction: this is handled via a websocket, allowing the agent state to be shared live (thinking, responding) and the connected client to be able to manage the session (e.g., stop agent, if it's thinking)