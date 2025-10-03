## v0.12.5 (2025-10-03)

### Fix

- **deps**: update dependency mcp to >=1.16,<1.17

## v0.12.4 (2025-09-25)

### Fix

- **deps**: update dependency mcp to >=1.15,<1.16

## v0.12.3 (2025-09-23)

### Refactor

- Add tools for all resources to enable tool-only workflows

## v0.12.2 (2025-09-20)

### Refactor

- Add `http` to --transport option

## v0.12.1 (2025-09-11)

### Fix

- **docker**: Provide --host 0.0.0.0 in default docker image

## v0.12.0 (2025-09-11)

### Feat

- **server**: Add support for `streamable-http` transport type

## v0.11.1 (2025-09-11)

### Fix

- **deps**: update dependency mcp to >=1.13,<1.14

## v0.11.0 (2025-09-11)

### Feat

- **deck**: Add support for stack, cards, labels
- **deck**: Initialize Deck app client/server

## v0.10.0 (2025-09-10)

### Feat

- Add WebDAV resource copy functionality
- Add WebDAV resource move/rename functionality

## v0.9.0 (2025-09-10)

### BREAKING CHANGE

- FASTMCP_-prefixed env vars have been replaced by CLI
arguments. Refer to the README for updated usage.

### Feat

- **cli**: Replace `mcp run` with click CLI and runtime options

## v0.8.3 (2025-08-31)

### Fix

- **server**: Replace ErrorResponses with standard McpErrors
- **notes**: Include ETags in responses to avoid accidently updates

## v0.8.2 (2025-08-31)

### Fix

- **notes**: Remove note contents from responses to reduce token usage

## v0.8.1 (2025-08-30)

### Fix

- **model**: Serialize timestamps in RFC3339 format

## v0.8.0 (2025-08-30)

### Feat

- **client**: Preserve fields when modifying contacts/calendar resources
- **server**: Add structured output to all tool/resource output

### Refactor

- Use _make_request where available

## v0.7.2 (2025-08-30)

### Fix

- **client**: Use paging to fetch all notes

## v0.7.1 (2025-08-08)

### Fix

- **client**: Strip cookies from responses to avoid falsely raising CSRF errors

## v0.7.0 (2025-08-03)

### Feat

- **contacts**: Initialize Contacts App

## v0.6.1 (2025-08-01)

### Fix

- **calendar**: Fix iCalendar date vs datetime format
- **calendar**: Remove try/except in calendar API

## v0.6.0 (2025-07-29)

### Feat

- **calendar**: add comprehensive Calendar app support via CalDAV protocol

### Fix

- apply ruff formatting to pass CI checks
- **calendar**: address PR feedback from maintainer

### Refactor

- **calendar**: optimize logging for production readiness

## v0.5.0 (2025-07-26)

### Feat

- Update webdav client create_directory method to handle recursive directories
- **webdav**: add complete file system support

### Fix

- apply ruff formatting to test_webdav_operations.py

## v0.4.1 (2025-07-10)

### Fix

- **deps**: update dependency mcp to >=1.10,<1.11

## v0.4.0 (2025-07-06)

### Feat

- Add TablesClient and associated tools

### Fix

- update tests

### Refactor

- Modularize NC and Notes app client

## v0.3.0 (2025-06-06)

### Feat

- Switch to using async client

## v0.2.5 (2025-05-25)

### Fix

- Commitizen release process

## v0.2.4 (2025-05-25)

### Fix

- Do not update dependencies when running in Dockerfile
- Configure logging

## v0.2.3 (2025-05-25)

### Fix

- Limit search results to notes with score > 0.5

## v0.2.2 (2025-05-24)

### Fix

- Install deps before checking service

## v0.2.1 (2025-05-24)

### Fix

- Install deps before checking service

## v0.2.1 (2025-05-24)

## v0.2.0 (2025-05-24)

### Feat

- **notes**: Add append to note functionality

### Fix

- **deps**: update dependency mcp to >=1.9,<1.10

## v0.1.3 (2025-05-16)

## v0.1.2 (2025-05-05)

## v0.1.1 (2025-05-05)

## v0.1.0 (2025-05-05)
