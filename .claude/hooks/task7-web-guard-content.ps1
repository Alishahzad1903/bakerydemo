# PreToolUse hook: the CONTENT arm's variant of the task-7 guard.
#
# TASK-7 CONTENT ARM ONLY (2026-09-09). This arm's knowledge source is VideoGen's own
# documentation: a staged index file naming the pages, plus the pages themselves. Blocking
# docs.videogen.io would leave the arm with titles and one-line summaries and no page bodies,
# which is not the condition the arm is meant to measure (owner decision, recorded in
# knowledge/task7-videogen.md).
#
# So this file is task7-web-guard.ps1 with ONE hole: a WebFetch of a docs.videogen.io URL is
# allowed. Everything else VideoGen-related is denied exactly as for the other three arms -
# including WebSearch, and including videogen.io / app.videogen.io / any third-party write-up.
#
# WARNING >> THIS IS THE ONLY TOOL-POLICY DIFFERENCE BETWEEN THE FOUR TASK-7 ARMS, and it must be
# disclosed with any comparative claim (RULES.md rule 1). The content arm may reach the network
# for its source; the plugin and skills arms cannot, because theirs is already in the workspace;
# the mcp arm cannot either, though its SERVER fetches the same docs from inside its own process,
# which no PreToolUse hook can see.

$raw = [Console]::In.ReadToEnd()
try { $evt = $raw | ConvertFrom-Json } catch { exit 0 }

if ($evt.tool_name -ne 'WebSearch' -and $evt.tool_name -ne 'WebFetch') { exit 0 }

$parts = @()
if ($evt.tool_input.query)  { $parts += [string]$evt.tool_input.query }
if ($evt.tool_input.url)    { $parts += [string]$evt.tool_input.url }
if ($evt.tool_input.prompt) { $parts += [string]$evt.tool_input.prompt }
$text = $parts -join ' '

# The permitted hole: a WebFetch whose URL is on the documentation host. Checked on the URL
# field alone and on the HOST, so a WebSearch mentioning the host in its query does not pass,
# and neither does a fetch of some other host that merely names docs.videogen.io in a prompt.
if ($evt.tool_name -eq 'WebFetch' -and $evt.tool_input.url) {
    $u = $null
    if ([System.Uri]::TryCreate([string]$evt.tool_input.url, [System.UriKind]::Absolute, [ref]$u)) {
        if ($u.Host -eq 'docs.videogen.io') { exit 0 }
    }
}

$blocklist = 'videogen'

if ($text -match $blocklist) {
    $decision = @{
        hookSpecificOutput = @{
            hookEventName            = 'PreToolUse'
            permissionDecision       = 'deny'
            permissionDecisionReason = 'Only VideoGen''s own documentation at docs.videogen.io may be fetched in this workspace. Other VideoGen lookups are not permitted; use the documentation index provided inside the workspace.'
        }
    }
    Write-Output ($decision | ConvertTo-Json -Depth 5 -Compress)
}
exit 0
