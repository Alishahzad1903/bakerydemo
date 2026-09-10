# PreToolUse hook: deny WebSearch/WebFetch calls whose query/URL touches the task-7
# provider blocklist (VideoGen only). All other lookups (Django, Wagtail, Python generally,
# ...) pass through untouched.
#
# TASK-7 VARIANT (2026-09-09). Task 7 has ONE provider, so the blocklist is VideoGen-only:
# a task-7 run has no Maxio/PayPal/Twilio/Firecrawl/Visa leg, and blocking terms the task never
# touches would only add noise to the transcript audit. It is a SEPARATE file (selected per arm
# via webGuardFile in the arm config), matching the task-3/4/5/6 convention.
#
# USED BY THREE OF THE FOUR ARMS: plugin, mcp and skills. The CONTENT arm uses
# task7-web-guard-content.ps1 instead, which permits docs.videogen.io because that documentation
# IS that arm's knowledge source. That asymmetry is deliberate, is the only tool-policy
# difference between the arms, and must be disclosed with any comparative claim.
#
# 'videogen' covers its docs, app and API hosts (videogen.io, docs.videogen.io,
# app.videogen.io, api.videogen.io). Keyword filtering is best-effort; the transcript audit is
# the backstop.
#
# WARNING: THE GUARD DOES NOT CONSTRAIN THE MCP ARM'S SERVER. docs.videogen.io/_mcp/server fetches
# documentation inside its own process, and a PreToolUse hook only sees the agent's own
# WebFetch/WebSearch calls. Same caveat every docs-MCP arm in this repo carries.

$raw = [Console]::In.ReadToEnd()
try { $evt = $raw | ConvertFrom-Json } catch { exit 0 }

if ($evt.tool_name -ne 'WebSearch' -and $evt.tool_name -ne 'WebFetch') { exit 0 }

$parts = @()
if ($evt.tool_input.query)  { $parts += [string]$evt.tool_input.query }
if ($evt.tool_input.url)    { $parts += [string]$evt.tool_input.url }
if ($evt.tool_input.prompt) { $parts += [string]$evt.tool_input.prompt }
$text = $parts -join ' '

# Deliberately NOT blocked: the live VideoGen API reached via Bash/curl or the SDK
# (api.videogen.io) - self-verification against the live account is part of the task for every
# arm; this guard only closes the WebFetch/WebSearch DOCUMENTATION channel.
$blocklist = 'videogen'

if ($text -match $blocklist) {
    $decision = @{
        hookSpecificOutput = @{
            hookEventName            = 'PreToolUse'
            permissionDecision       = 'deny'
            permissionDecisionReason = 'VideoGen-related web lookups are not permitted in this workspace. Use only the knowledge sources provided inside the workspace.'
        }
    }
    Write-Output ($decision | ConvertTo-Json -Depth 5 -Compress)
}
exit 0
