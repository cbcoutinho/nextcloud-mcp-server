# MCP Server Comparison: Nextcloud MCP Server vs Context Agent

This document compares the two MCP server implementations in the Nextcloud ecosystem:

1. **Nextcloud MCP Server** (this project) - Standalone MCP server for external access to Nextcloud
2. **Context Agent MCP Server** - MCP server embedded within Nextcloud as an External App

## Executive Summary

Both projects expose Nextcloud functionality via the Model Context Protocol (MCP), but serve different purposes and audiences:

- **Nextcloud MCP Server**: Brings Nextcloud OUT to external MCP clients (Claude Code, etc.)
- **Context Agent**: Brings external MCP servers IN to Nextcloud's AI Assistant

## Architecture Overview

```mermaid
graph TB
    subgraph External["External Clients"]
        CC[Claude Code]
        IDE[IDEs with MCP]
        APP[Other MCP Clients]
    end

    subgraph NMCP["Nextcloud MCP Server<br/>(This Project)"]
        NMCP_Server[FastMCP Server]
        NMCP_Client[HTTP Clients]
        NMCP_Auth[OAuth/BasicAuth]
    end

    subgraph NC["Nextcloud Instance"]
        subgraph CA["Context Agent ExApp"]
            CA_Agent[LangGraph Agent]
            CA_MCP[MCP Server /mcp]
            CA_Tools[Tool Loader]
        end

        NC_Apps[Nextcloud Apps<br/>Notes, Calendar, Files, etc.]
        NC_Assistant[Assistant App]
    end

    subgraph ExtMCP["External MCP Servers"]
        Weather[Weather MCP]
        Other[Other Services]
    end

    %% External clients connect to standalone MCP server
    CC --> NMCP_Server
    IDE --> NMCP_Server
    APP --> NMCP_Server

    %% Standalone MCP server talks to Nextcloud over HTTP
    NMCP_Server --> NMCP_Auth
    NMCP_Auth --> NMCP_Client
    NMCP_Client -->|HTTP/HTTPS| NC_Apps

    %% Context Agent is inside Nextcloud
    CA_Agent --> CA_Tools
    CA_Tools --> NC_Apps
    CA_MCP -->|Exposes to| NC_Assistant
    NC_Assistant -->|User requests| CA_Agent

    %% Context Agent can consume external MCP servers
    CA_Tools -->|Consumes| ExtMCP

    %% Context Agent could consume Nextcloud MCP Server
    CA_Tools -.->|Could consume| NMCP_Server

    classDef external fill:#e1f5ff
    classDef standalone fill:#fff4e1
    classDef internal fill:#e8f5e9

    class CC,IDE,APP external
    class NMCP_Server,NMCP_Client,NMCP_Auth standalone
    class CA_Agent,CA_MCP,CA_Tools,NC_Apps,NC_Assistant internal
```

## Deployment Models

```mermaid
graph LR
    subgraph Deploy1["Nextcloud MCP Server Deployment"]
        direction TB
        D1[Docker Container]
        D2[Cloud VM]
        D3[Local Machine]
        D4[Kubernetes Pod]
    end

    subgraph Deploy2["Context Agent Deployment"]
        direction TB
        NC[Nextcloud Instance<br/>with AppAPI]
        ExApp[External App Container<br/>Managed by Nextcloud]
    end

    Deploy1 -.->|HTTP/HTTPS| NC
    ExApp -->|Integrated| NC

    classDef deploy fill:#fff4e1
    classDef integrated fill:#e8f5e9

    class D1,D2,D3,D4 deploy
    class NC,ExApp integrated
```

### Nextcloud MCP Server
- **Location**: Runs anywhere with network access to Nextcloud
- **Deployment**: Docker, VM, local machine, Kubernetes
- **Connection**: HTTP/HTTPS to Nextcloud APIs
- **Independence**: Fully standalone service

### Context Agent
- **Location**: Runs inside Nextcloud as External App
- **Deployment**: Managed by Nextcloud AppAPI
- **Connection**: Native nc-py-api integration
- **Integration**: Deep Nextcloud integration

## Authentication Architecture

```mermaid
graph TB
    subgraph NMCP_Auth["Nextcloud MCP Server Authentication"]
        direction TB
        Client1[MCP Client]

        subgraph BasicAuth["BasicAuth Mode"]
            BA_Shared[Shared NextcloudClient]
            BA_Creds[Username + Password]
        end

        subgraph OAuth["OAuth Mode"]
            OAuth_Token[OAuth Token]
            OAuth_Verify[Token Verifier]
            OAuth_OIDC[OIDC Discovery]
            OAuth_Client[Per-Request Client]
        end

        Client1 -->|Basic Auth| BasicAuth
        Client1 -->|Bearer Token| OAuth
        BA_Creds --> BA_Shared
        OAuth_Token --> OAuth_Verify
        OAuth_OIDC --> OAuth_Verify
        OAuth_Verify --> OAuth_Client
    end

    subgraph CA_Auth["Context Agent Authentication"]
        direction TB
        Client2[MCP Client]
        CA_Header[Authorization Header]
        CA_OCS[OCS API Validation]
        CA_User[User Context]
        CA_NC[nc-py-api Client]

        Client2 --> CA_Header
        CA_Header --> CA_OCS
        CA_OCS -->|Extract user_id| CA_User
        CA_User -->|nc.set_user| CA_NC
    end

    classDef auth fill:#fff4e1
    classDef user fill:#e1f5ff

    class BasicAuth,OAuth auth
    class CA_User user
```

## Tool Registration & Loading

```mermaid
sequenceDiagram
    participant Startup
    participant NMCP as Nextcloud MCP<br/>Server
    participant CA as Context Agent
    participant Request as Client Request

    Note over Startup,NMCP: Nextcloud MCP Server (Static)
    Startup->>NMCP: Server starts
    NMCP->>NMCP: configure_notes_tools(mcp)
    NMCP->>NMCP: configure_calendar_tools(mcp)
    NMCP->>NMCP: configure_contacts_tools(mcp)
    Note over NMCP: Tools registered once<br/>at startup
    Request->>NMCP: Call tool
    NMCP->>NMCP: Use pre-registered tool

    Note over Startup,CA: Context Agent (Dynamic)
    Startup->>CA: Server starts
    CA->>CA: Install ToolListMiddleware
    Request->>CA: List tools (or 60s elapsed)
    CA->>CA: get_tools(nc)
    CA->>CA: Import all_tools/*.py
    CA->>CA: Call module.get_tools(nc)
    CA->>CA: Regenerate tool functions
    Note over CA: Tools refreshed every 60s<br/>or on demand
    Request->>CA: Call tool
    CA->>CA: Regenerate with fresh nc
```

## Tool Definition Patterns

### Nextcloud MCP Server

```python
# Static registration at startup
def configure_notes_tools(mcp: FastMCP):
    @mcp.tool()
    async def nc_notes_create_note(
        title: str,
        content: str,
        category: str,
        ctx: Context
    ) -> CreateNoteResponse:
        """Create a new note"""
        client = get_client(ctx)  # Auto-detects auth mode
        note_data = await client.notes.create_note(
            title=title,
            content=content,
            category=category
        )
        return CreateNoteResponse(
            id=note_data["id"],
            title=note_data["title"],
            etag=note_data["etag"]
        )

    # Resources for structured data access
    @mcp.resource("nc://Notes/{note_id}")
    async def nc_get_note_resource(note_id: int):
        """Get user note using note id"""
        ctx = mcp.get_context()
        client = get_client(ctx)
        note_data = await client.notes.get_note(note_id)
        return Note(**note_data)
```

**Key Features**:
- Native FastMCP `@mcp.tool()` decorator
- Pydantic models for type safety
- MCP Resources support
- Comprehensive error handling with McpError
- Context-based client resolution

### Context Agent

```python
# Dynamic loading at runtime
async def get_tools(nc: Nextcloud):
    @tool
    @safe_tool
    def list_calendars():
        """List all existing calendars by name"""
        principal = nc.cal.principal()
        calendars = principal.calendars()
        return ", ".join([cal.name for cal in calendars])

    @tool
    @dangerous_tool
    def schedule_event(
        calendar_name: str,
        title: str,
        description: str,
        start_date: str,
        end_date: str,
        attendees: list[str] | None,
        start_time: str | None,
        end_time: str | None
    ):
        """Create a new event or meeting in a calendar"""
        # Parse dates and times
        start_datetime = datetime.strptime(start_date, "%Y-%m-%d")
        # ... event creation logic
        principal = nc.cal.principal()
        calendar = {cal.name: cal for cal in calendars}[calendar_name]
        calendar.add_event(str(c))
        return True

    return [list_calendars, schedule_event, ...]

def get_category_name():
    return "Calendar and Tasks"

def is_available(nc: Nextcloud):
    return True  # or check capabilities
```

**Key Features**:
- LangChain `@tool` decorator
- `@safe_tool` / `@dangerous_tool` decorators
- Dynamic tool regeneration with fresh context
- Tools returned as list from async function
- Availability checking per module

## Client Architecture

```mermaid
graph TB
    subgraph NMCP_Client["Nextcloud MCP Server Clients"]
        direction TB
        NMCP_Main[NextcloudClient]
        NMCP_Base[BaseNextcloudClient]

        NMCP_Notes[NotesClient]
        NMCP_Cal[CalendarClient]
        NMCP_Contacts[ContactsClient]
        NMCP_Tables[TablesClient]
        NMCP_WebDAV[WebDAVClient]
        NMCP_Deck[DeckClient]

        NMCP_Main --> NMCP_Notes
        NMCP_Main --> NMCP_Cal
        NMCP_Main --> NMCP_Contacts
        NMCP_Main --> NMCP_Tables
        NMCP_Main --> NMCP_WebDAV
        NMCP_Main --> NMCP_Deck

        NMCP_Notes -.->|extends| NMCP_Base
        NMCP_Cal -.->|extends| NMCP_Base
        NMCP_Contacts -.->|extends| NMCP_Base

        NMCP_Base --> HTTPX["httpx.AsyncClient"]
        NMCP_Base --> Retry["@retry_on_429"]
    end

    subgraph CA_Client["Context Agent Client"]
        direction TB
        CA_NC["nc-py-api<br/>NextcloudApp"]

        CA_NC --> CA_Cal["nc.cal<br/>CalDAV"]
        CA_NC --> CA_Talk["nc.talk<br/>Talk API"]
        CA_NC --> CA_OCS["nc.ocs<br/>OCS API"]
        CA_NC --> CA_Session["nc._session<br/>HTTP Adapter"]
    end

    HTTPX -->|"HTTP/HTTPS"| NextcloudAPI["Nextcloud APIs"]
    CA_Session -->|"HTTP/HTTPS"| NextcloudAPI

    classDef custom fill:#fff4e1
    classDef native fill:#e8f5e9

    class NMCP_Main,NMCP_Base,NMCP_Notes,NMCP_Cal custom
    class CA_NC,CA_Cal,CA_Talk,CA_OCS native
```

## Functionality Comparison

### Available Tools & Features

| Feature Category | Nextcloud MCP Server | Context Agent MCP |
|-----------------|---------------------|-------------------|
| **Notes** | ✅ Full CRUD, search, attachments (7 tools) | ❌ Not implemented |
| **Calendar** | ✅ Full CalDAV (events, recurring, attendees) | ✅ Schedule events, list calendars, free/busy, tasks (4 tools) |
| **Contacts** | ✅ Full CardDAV (address books, contacts) | ✅ Find person, current user details (2 tools) |
| **Files** | ✅ Full WebDAV (read, write, directories) | ✅ Get content, folder tree, sharing (3 tools) |
| **Tables** | ✅ Row CRUD operations | ❌ Not implemented |
| **Deck** | ✅ Boards, stacks, cards | ✅ Create board, add card (2 tools) |
| **Talk** | ❌ Not implemented | ✅ List/send messages, create conversation (4 tools) |
| **Mail** | ❌ Not implemented | ✅ Send email, list mailboxes (2 tools) |
| **AI Features** | ❌ Not implemented | ✅ Image gen, audio2text, doc-gen, context_chat (4 tools) |
| **Web Search** | ❌ Not implemented | ✅ DuckDuckGo, YouTube search (2 tools) |
| **Location** | ❌ Not implemented | ✅ OpenStreetMap, HERE transit, weather (3 tools) |
| **OpenProject** | ❌ Not implemented | ✅ Integration (2 tools) |
| **MCP Resources** | ✅ notes://, nc:// URIs | ❌ Not supported |
| **External MCP** | ❌ Pure server only | ✅ Consumes external MCP servers |
| **Sharing** | ✅ Share management API | ❌ Not implemented |
| **Capabilities** | ✅ Server info resource | ❌ Not exposed |

### Tool Count Summary

- **Nextcloud MCP Server**: ~50+ tools and resources
  - Deep integration with specific apps
  - Full CRUD operations
  - MCP Resources for structured data

- **Context Agent**: ~28+ tools
  - Broader feature coverage
  - Action-oriented (agent tasks)
  - Can aggregate external MCP servers

## Tool Safety & Confirmation

### Context Agent Safety Model

```mermaid
graph TD
    Request[User Request] --> Agent[LangGraph Agent]
    Agent --> Model[LLM generates tool calls]
    Model --> Check{Tool type?}

    Check -->|"@safe_tool"| Execute[Execute immediately]
    Check -->|"@dangerous_tool"| Queue[Queue for confirmation]

    Queue --> UserNode[Request user confirmation]
    UserNode -->|Approved| Execute
    UserNode -->|Denied| Cancel[Cancel with reason]

    Execute --> Result[Return result to agent]
    Cancel --> Result

    Result --> Agent

    classDef safe fill:#e8f5e9
    classDef danger fill:#ffe8e8

    class Execute safe
    class Queue,UserNode,Cancel danger
```

**Safe Tools** (read-only):
- `list_calendars`
- `find_person_in_contacts`
- `list_talk_conversations`
- `get_file_content`
- `get_folder_tree`

**Dangerous Tools** (write operations):
- `schedule_event`
- `send_message_to_conversation`
- `create_public_sharing_link`
- `send_email`

### Nextcloud MCP Server Safety

**No built-in safety classification**:
- All tools treated equally
- Relies on MCP client for validation
- OAuth scopes could control permissions
- User must review all actions

## Error Handling

### Nextcloud MCP Server

```python
try:
    note_data = await client.notes.create_note(...)
    return CreateNoteResponse(...)
except HTTPStatusError as e:
    if e.response.status_code == 403:
        raise McpError(ErrorData(
            code=-1,
            message="Access denied: insufficient permissions"
        ))
    elif e.response.status_code == 413:
        raise McpError(ErrorData(
            code=-1,
            message="Note content too large"
        ))
    elif e.response.status_code == 409:
        raise McpError(ErrorData(
            code=-1,
            message="Note with this title already exists"
        ))
```

**Features**:
- Comprehensive HTTP status code handling
- User-friendly error messages
- Specific error codes
- Guidance on resolution

### Context Agent

```python
def schedule_event(...):
    """Create event"""
    # ... implementation
    calendar.add_event(str(c))
    return True  # Simple boolean return
```

**Features**:
- Minimal error handling
- Exceptions propagate to agent
- LangChain handles retries
- Agent interprets failures

## Use Cases

### When to Use Nextcloud MCP Server

```mermaid
graph LR
    Root[Nextcloud MCP Server]

    Root --> ExtAccess[External Access]
    Root --> OAuth[OAuth Security]
    Root --> DeepAPI[Deep API Access]
    Root --> Deploy[Standalone Deployment]

    ExtAccess --> EA1[Claude Code integration]
    ExtAccess --> EA2[IDE plugins with MCP]
    ExtAccess --> EA3[Custom MCP clients]
    ExtAccess --> EA4[Cross-platform tools]

    OAuth --> O1[Token-based auth]
    OAuth --> O2[OIDC compliance]
    OAuth --> O3[Per-user permissions]
    OAuth --> O4[Secure external access]

    DeepAPI --> DA1[Full CRUD operations]
    DeepAPI --> DA2[Notes management]
    DeepAPI --> DA3[Calendar CalDAV]
    DeepAPI --> DA4[Contacts CardDAV]
    DeepAPI --> DA5[File operations]
    DeepAPI --> DA6[Table data]

    Deploy --> D1[Docker containers]
    Deploy --> D2[Cloud VMs]
    Deploy --> D3[Kubernetes]
    Deploy --> D4[On-premise servers]

    classDef rootStyle fill:#4a90e2,stroke:#2e5c8a,color:#fff
    classDef categoryStyle fill:#f39c12,stroke:#d68910,color:#fff
    classDef itemStyle fill:#e8f5e9,stroke:#81c784

    class Root rootStyle
    class ExtAccess,OAuth,DeepAPI,Deploy categoryStyle
    class EA1,EA2,EA3,EA4,O1,O2,O3,O4,DA1,DA2,DA3,DA4,DA5,DA6,D1,D2,D3,D4 itemStyle
```

**Best for**:
1. External clients accessing Nextcloud (Claude Code, IDEs)
2. OAuth/OIDC authentication requirements
3. Full CRUD on Notes, Calendar, Contacts, Tables
4. WebDAV file system access
5. MCP Resources for structured data
6. Flexible deployment scenarios
7. Building external integrations

### When to Use Context Agent MCP Server

```mermaid
graph LR
    Root[Context Agent MCP]

    Root --> Assistant[AI Assistant]
    Root --> ActionOriented[Action-Oriented]
    Root --> MCPAgg[MCP Aggregation]
    Root --> Safety[Safety Features]

    Assistant --> A1[Nextcloud UI integration]
    Assistant --> A2[Task Processing API]
    Assistant --> A3[User requests in Assistant]
    Assistant --> A4[Human-in-the-loop]

    ActionOriented --> AO1[Send emails]
    ActionOriented --> AO2[Create calendar events]
    ActionOriented --> AO3[Post Talk messages]
    ActionOriented --> AO4[Generate images]
    ActionOriented --> AO5[Search web]

    MCPAgg --> M1[Consume external MCP servers]
    MCPAgg --> M2[Weather services]
    MCPAgg --> M3[Maps and transit]
    MCPAgg --> M4[Custom integrations]
    MCPAgg --> M5[Unified tool interface]

    Safety --> S1[Read operations auto-execute]
    Safety --> S2[Write operations require approval]
    Safety --> S3[User confirmation flow]
    Safety --> S4[Agent safety]

    classDef rootStyle fill:#9b59b6,stroke:#6c3483,color:#fff
    classDef categoryStyle fill:#e74c3c,stroke:#c0392b,color:#fff
    classDef itemStyle fill:#fff4e1,stroke:#f39c12

    class Root rootStyle
    class Assistant,ActionOriented,MCPAgg,Safety categoryStyle
    class A1,A2,A3,A4,AO1,AO2,AO3,AO4,AO5,M1,M2,M3,M4,M5,S1,S2,S3,S4 itemStyle
```

**Best for**:
1. AI-driven actions inside Nextcloud UI
2. Assistant app integration
3. Safe/dangerous tool distinction
4. Talk, Mail, Deck operations
5. AI features (image gen, audio2text)
6. Web search and maps
7. Aggregating external MCP servers
8. Agent acting on behalf of users

## Complementary Architecture

The two MCP servers can work together in complementary ways:

```mermaid
graph TB
    User[User] -->|Requests AI assistance| Assistant[Nextcloud Assistant App]

    Assistant --> ContextAgent[Context Agent]

    subgraph ContextAgent["Context Agent (Inside Nextcloud)"]
        direction TB
        Agent[LangGraph Agent]
        MCPServer[MCP Server /mcp]
        ToolLoader[Tool Loader]

        Agent --> ToolLoader
        ToolLoader --> InternalTools[Internal Tools<br/>Talk, Mail, Calendar]
    end

    subgraph ExternalMCP["External MCP Ecosystem"]
        NextcloudMCP[Nextcloud MCP Server<br/>This Project]
        WeatherMCP[Weather MCP]
        CustomMCP[Custom MCP Services]
    end

    ToolLoader -->|Consumes| NextcloudMCP
    ToolLoader -->|Consumes| WeatherMCP
    ToolLoader -->|Consumes| CustomMCP

    subgraph ExternalClients["External Clients"]
        Claude[Claude Code]
        IDE[IDEs with MCP]
    end

    Claude -->|Direct access| NextcloudMCP
    IDE -->|Direct access| NextcloudMCP

    NextcloudMCP -->|OAuth/HTTP| NextcloudApps[Nextcloud Apps<br/>Notes, Calendar, Files]
    InternalTools -->|nc-py-api| NextcloudApps

    classDef internal fill:#e8f5e9
    classDef external fill:#e1f5ff
    classDef mcp fill:#fff4e1

    class Assistant,Agent,MCPServer,ToolLoader,InternalTools,NextcloudApps internal
    class Claude,IDE external
    class NextcloudMCP,WeatherMCP,CustomMCP mcp
```

### Example Workflows

**Workflow 1: External Client → Nextcloud MCP Server**
```
Claude Code → Nextcloud MCP Server → Nextcloud Notes API
```
- User asks Claude Code to search notes
- Claude Code calls `nc_notes_search_notes` tool
- Returns results directly to user

**Workflow 2: Assistant → Context Agent → Internal Tools**
```
User → Assistant → Context Agent → Send Email Tool
```
- User asks Assistant to send an email
- Context Agent identifies "send_email" as dangerous
- Requests user confirmation
- Sends email via nc-py-api

**Workflow 3: Assistant → Context Agent → External MCP**
```
User → Assistant → Context Agent → Nextcloud MCP Server → Notes
```
- User asks Assistant about notes
- Context Agent consumes Nextcloud MCP Server as external MCP
- Gets notes data via MCP protocol
- Returns to user via Assistant

## Technical Comparison Matrix

| Aspect | Nextcloud MCP Server | Context Agent MCP |
|--------|---------------------|-------------------|
| **Framework** | FastMCP (native) | FastMCP + LangChain |
| **Tool Decorator** | `@mcp.tool()` | `@tool` from LangChain |
| **Tool Loading** | Static (startup) | Dynamic (runtime) |
| **Tool Refresh** | No (restart required) | Every 60 seconds |
| **Resources** | Yes (`@mcp.resource()`) | No |
| **Transports** | SSE, HTTP, Streamable-HTTP | Stateless HTTP only |
| **MCP Mode** | Server only | Server + Client (hybrid) |
| **Client Type** | httpx (custom HTTP) | nc-py-api (native) |
| **Deployment** | Standalone external | Inside Nextcloud (ExApp) |
| **Auth** | BasicAuth or OAuth/OIDC | Session-based (ExApp) |
| **User Context** | Shared or per-token | Per-request `nc.set_user()` |
| **Error Handling** | McpError with codes | Basic exceptions |
| **Type Safety** | Pydantic models | Python types |
| **Safety Model** | No built-in | Safe/Dangerous classification |
| **Dependencies** | FastMCP, httpx, Pydantic | nc-py-api, LangChain, LangGraph |
| **Integration** | HTTP APIs | AppAPI + Task Processing |
| **External MCP** | No | Yes (consumes) |

## Summary

Both MCP servers serve important but different roles in the Nextcloud ecosystem:

### Nextcloud MCP Server (This Project)
- **Purpose**: Expose Nextcloud to external MCP clients
- **Strength**: Deep CRUD operations, OAuth security, standalone deployment
- **Audience**: External developers, Claude Code users, integration builders

### Context Agent MCP Server
- **Purpose**: Bring AI agent capabilities to Nextcloud users
- **Strength**: Action-oriented, safe/dangerous tools, MCP aggregation
- **Audience**: Nextcloud users via Assistant app, AI-driven workflows

**Key Insight**: These are complementary, not competing. Context Agent could even consume Nextcloud MCP Server as one of its external MCP sources, creating a unified ecosystem where:
- External clients access Nextcloud via Nextcloud MCP Server
- Internal users leverage Context Agent for AI assistance
- Context Agent aggregates both internal tools and external MCP servers (including Nextcloud MCP Server)
