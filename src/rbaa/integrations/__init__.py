"""External activity fetchers (GitHub #7, Jira #8).

Each fetcher is gated per-resource by `rbaa.security.require_permission` and returns a typed
result instead of raising for ordinary per-resource failures (permission denial, 404). See
`rbaa.integrations.github` for the GitHub fetcher.
"""
