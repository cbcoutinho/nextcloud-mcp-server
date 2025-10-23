## v0.19.0 (2025-10-23)

### Feat

- Enable token introspection for opaque tokens

### Fix

- Add CORS middleware to allow browser-based clients like MCP Inspector

## v0.18.0 (2025-10-23)

### Feat

- **server**: Add support for custom OIDC scopes and permissions via JWTs
- Initialize JWT-scoped tools

### Fix

- Use occ-created OAuth clients with allowed_scopes for all tests
- Separate OAuth fixtures for opaque vs JWT tokens

### Refactor

- Update JWT client to use DCR, re-enable tool filtering

## v0.17.1 (2025-10-20)

### Fix

- **caldav**: Fix caldav search() due to missing todos

## v0.17.0 (2025-10-19)

### Feat

- **caldav**: Add support for tasks

### Fix

- **caldav**: Check that calendar exists after creation to avoid race condition
- **caldav**: Properly parse datetimes as vDDDTypes

### Refactor

- Migrate from internal CalendarClient to caldav library

## v0.16.0 (2025-10-19)

### Feat

- **webdav**: Add search and list favorite response tools

### Perf

- **notes**: Improve notes search performance using async iterators

## v0.15.2 (2025-10-17)

### Refactor

- Unify logging & remove factory deployment

## v0.15.1 (2025-10-17)

### Fix

- Increase HTTP client timeout to 30s
- Handle RequestError in mcp tools

## v0.15.0 (2025-10-17)

### Feat

- **cookbook**: Add full Cookbook app support with 13 tools and 2 resources

## v0.14.3 (2025-10-17)

### Fix

- **deps**: update dependency mcp to >=1.18,<1.19

## v0.14.2 (2025-10-16)

### Fix

- **deps**: update dependency pillow to v12

## v0.14.1 (2025-10-15)

### Fix

- **oauth**: Remove the option to force_register new clients

## v0.14.0 (2025-10-15)

### Feat

- Add Groups API client
- add sharing API client and server tools
- **users**: Initialize user API client

### Fix

- Update user/groups API to OCS v2

## v0.13.0 (2025-10-13)

### Feat

- **server**: Experimental support for OAuth2/OIDC authentication

## v0.12.6 (2025-10-11)

### Fix

- **deps**: update dependency mcp to >=1.17,<1.18

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
