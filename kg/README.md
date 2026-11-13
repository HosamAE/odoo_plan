> **This copy belongs to these tools (2026-11-08).** The engine used to stand alone in
> `C:\odoo\odoo-kg` (repository hasanshamia/odoo-kg, commit 65a07f5). Every Odoo version now
> carries its own copy in `C:\odoo\odoo<NN>\tools\kg`, like the rest of its tools; a change made
> here reaches another version only by cherry-pick. Project configs: `kg\config\local\<project>.toml`
> (never committed); graph data: `%LOCALAPPDATA%\odoo-kg\projects\<project>`.

# odoo-kg — Odoo Knowledge Graph

A deterministic, read-only indexer that builds a SQLite knowledge graph of an
Odoo repository, plus a local MCP server that answers small, bounded questions
about it.

* **M1** — the indexer: modules and their manifest dependencies.
* **M2** — the MCP server: seven read-only tools over the graph M1 built.

Standard library only. No dependencies are installed to run or test this tool,
and the MCP server implements the protocol directly rather than through an SDK.

---

## Working in a team

**The program is shared. The graph is not.**

`odoo-kg` is a tool everyone runs from the same checkout. The graph it builds
is local to whoever built it: it lives under `%LOCALAPPDATA%\odoo-kg\projects\`
on that person's machine, it describes that person's working tree at the moment
they built it, and it is never shared, copied between developers, or committed.

There is nothing to coordinate. Two developers do not have "the same graph out
of sync" — they have two graphs, each describing their own checkout.

### What must never be committed

* the database files (`graph.db`, any `generations\` directory)
* the `CURRENT` pointer
* `build.lock`
* anything else under the data directory

None of it is inside the repository, so there is nothing to add to
`.gitignore` — but do not move a data directory into the repository to work
around a path problem. A graph is a build artefact of one machine at one
moment; committing one would hand somebody else answers about a tree they do
not have.

Each developer's `data_dir` is their own. Configuration files carry it, so a
second developer copies a `config\*.toml` and changes two lines:

```toml
[project]
project_id = "your-own-project-id"

[storage]
data_dir = "C:/Users/<you>/AppData/Local/odoo-kg/projects/your-own-project-id"
```

`data_dir` **must be under `%LOCALAPPDATA%\odoo-kg\projects`**. That is the one
tree generated data may live in, and the write guard refuses anything else —
including a path elsewhere under your own `%LOCALAPPDATA%`. `doctor` prints the
allowed root, so start there if a path is rejected. No absolute path to
anybody's home directory is compiled into the program.

Give your project its own `project_id`. It is hashed into every node and edge
id, so two developers with different ids never produce comparable graphs by
accident — and reusing somebody else's id in your own `data_dir` would make two
unrelated graphs claim to be the same project.

### Building and publishing your graph

```powershell
python -m odoo_kg index --config config\your-project.toml
```

That is the whole flow. The run builds a new generation, checks it, and
publishes it. Anything reading the graph keeps using the previous generation
until it next opens one, and never sees a half-written file.

### Before Claude works on the code

Claude should call **`kg_status`** first, every session, before relying on any
answer from the graph. It reports `fresh` or `stale` and names the commit the
graph was built from. The same verdict is available on the command line as
`python -m odoo_kg status`.

If it says `stale`, the graph describes an older tree than the one on disk.
Answers about files that changed since then may be wrong — not missing, wrong,
which is worse. Run `index` and ask again.

### What happens if a build is interrupted

Nothing, to the published graph. A build writes into a new generation
directory and the pointer moves only at the very end, after every check has
passed. Interrupt it — Ctrl+C, a crash, a reboot — and:

* the published generation is exactly as it was, and everything keeps working;
* a partial generation directory is left behind, harmless and ignored;
* the next `index` run starts a fresh generation.

If a build was killed hard enough to leave `build.lock` behind, the next run
refuses and says so, naming the process and how long ago it started. Delete the
lock file it names once you are sure no build is running.

### Two builds at once

The second one is refused with a clear message. Builds are not interleaved and
never half-merge into each other.

### Cleaning up old generations

Old generations are kept on purpose. Nothing deletes them automatically,
because on Windows a file another process has open cannot be deleted, and a
tool that removes graphs a server might be reading is a tool that eventually
removes the wrong one.

To reclaim space:

1. Stop every MCP server and CLI command using that project.
2. `python -m odoo_kg generations --config ...` and note which one is marked
   `*` — that is the published one.
3. Delete the other `gen-*` directories by hand.

Never delete the published one, and never delete the `CURRENT` file.

## Other projects and Odoo versions

The acceptance floors in `verify.py` (module, model, XML ID, route counts and
the named vendor modules) were measured on testing19. Any other project would
fail them and never publish. A config therefore names its **acceptance
profile**:

```toml
[acceptance]
profile = "generic"   # or "testing19" (the default)
```

* `testing19` — the default, so every existing config keeps exactly the checks
  it had.
* `generic` — every integrity and honesty check still runs and still gates the
  publish (excluded paths, include roots, evidence vocabularies, bounded
  excerpts, dangling references...). The row-count floors are reported but not
  enforced, the named testing19 modules are not looked for, and off-series
  versions are listed rather than failed. A graph with no module at all still
  fails: that is a wrong `repo_root`, not a small project.

`odoo-ai workspace` (in `C:\odoo\odoo_ai_cli`) writes one config per project
under `config\local\` — ignored by Git, because it carries this machine's
`data_dir` — plus the VS Code tasks and a `.mcp.json` for Claude Code. Use it
for Odoo 17, 18, 19 and 20 projects alike.

## Known limitations in 0.4.0

Five things this release does not do, or does approximately. None of them is a
bug to be worked around quietly — each is a deliberate boundary, and knowing
where it sits is part of using the graph correctly.

**A blocked `CURRENT` can delay a publish.** Publishing swaps one small pointer
file, and on Windows that fails while another process holds it open — a virus
scanner, a backup agent, an editor. The publish retries for five seconds and
then refuses. Nothing is lost: the previous generation stays published and
usable, the finished new one stays on disk, and
`odoo-kg publish --generation <id>` promotes it later without indexing again.

**`verify`'s acceptance floors are tuned to this project.** Values like the
minimum module count were measured against testing19. A build that legitimately
drops below one — you deleted modules on purpose — fails its checks and is
therefore not published. The fix is to update the floor deliberately, not to
skip the check.

**Asset patterns are bounded at twelve wildcards.** Beyond that a pattern is
reported as too complex rather than evaluated, because some wildcard chains
cannot be simplified and take unbounded time to match. No real manifest comes
close; the deepest in testing19 uses two. Character classes (`[abc]`) are not
implemented at all and are reported as unsupported syntax.

**A method reference from a bare name is a guess.** When a document writes
`action_send_quotation` without naming a model, the graph binds it to the one
model defining that name today, marked `binding_mode: unique_bare_name` and
`binding_confidence: heuristic`. Read those fields. If a second model later
defines the same name, the next build records the reference as ambiguous and
binds it to nothing rather than keeping a stale guess.

**A stale build lock is described, never broken automatically.** A lock older
than an hour is *reported* as looking abandoned, but nothing removes it for
you. `odoo-kg unlock` requires the lock's exact id, refuses outright while the
holder is running, and needs `--force` only where the holder genuinely cannot
be checked from this machine.

## Requirements

| Requirement | Value | Why |
| --- | --- | --- |
| Python | **3.11+** (verified on 3.14.6) | `tomllib` is standard library from 3.11 |
| Platform | Windows | v1 targets Windows path semantics only |
| Network | none | the indexer never opens a socket |
| PostgreSQL | none | `installed_state` is therefore always `unknown` |

The version check is explicit: on Python < 3.11 the tool refuses to start
rather than reaching for a TOML backport.

---

## Commands

Run everything from the tool directory. No installation step is required —
`odoo_kg` is imported from the working directory.

```powershell
cd c:\odoo\tools\odoo-kg
```

### Validate the configuration and environment (writes nothing)

```powershell
python -m odoo_kg doctor --config config\sample-project.toml
```

### Build and publish a graph

```powershell
python -m odoo_kg index --config config\sample-project.toml
```

This writes a **new generation** — `generations\gen-<run_id>\graph.db` inside
your project's data directory — checks it, and only then publishes it by
swapping a small `CURRENT` pointer file. It never rewrites the graph that is
currently published, so an MCP server reading it is unaffected while the build
runs and simply picks up the new generation the next time it opens.

`--quiet` suppresses per-diagnostic output. `--rebuild` is accepted and no
longer means anything special: every run writes a new file, so every run is a
rebuild.

If the build fails its checks, nothing is published and the previous
generation stays current. The failed build is left on disk so you can look at
it.

### Inspect the published graph

```powershell
python -m odoo_kg stats --config config\sample-project.toml
```

### See which generation is published

```powershell
python -m odoo_kg published --config config\sample-project.toml
```

Prints the generation, its file, when it was published, the commit it was
built from, and re-checks the file against the manifest: size, sha256, SQLite
integrity, project, schema and run. Exit code `1` if any of that no longer
matches.

### Is the graph still fresh?

```powershell
python -m odoo_kg status --config config\sample-project.toml
```

`fresh` means the published graph describes the commit and working tree you
have now. `stale` means the tree has moved on: answers about the changed files
may be wrong. Fix it by running `index` again. Exit code `0` when fresh, `1`
when not.

### List the generations on disk

```powershell
python -m odoo_kg generations --config config\sample-project.toml
```

Lists what is there and marks the published one. **It deletes nothing.** See
[Cleaning up old generations](#cleaning-up-old-generations).

### Check the acceptance criteria against the published graph

```powershell
python -m odoo_kg verify --config config\sample-project.toml
```

Exit code is `0` when every check passes, `1` when one fails, `2` on a
configuration or environment error.

### Run the MCP server (M2)

```powershell
python -m odoo_kg serve --config config\sample-project.toml
```

The server speaks JSON-RPC 2.0 over stdio and waits for a client on stdin. Run
by hand it will simply sit there — that is correct. Diagnostics go to stderr;
stdout carries protocol messages and nothing else.

To drive it by hand for a quick check, pipe requests in:

```powershell
$req = @(
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18"}}'
  '{"jsonrpc":"2.0","method":"notifications/initialized"}'
  '{"jsonrpc":"2.0","id":2,"method":"tools/list"}'
  '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"kg_status","arguments":{}}}'
) -join "`n"
$req | python -m odoo_kg serve --config config\sample-project.toml
```

No Claude configuration and no `.mcp.json` are created by this repository;
wiring the server into a client is a separate, deliberate step.

### Run the test suite

```powershell
cd c:\odoo\tools\odoo-kg
python -m unittest discover -s tests -t . -v
```

The M1 acceptance suite indexes the real `testing19` working copy and skips
itself automatically if that repository is not present. **Every M2 test builds
its own temporary graph** under the approved data directory and never opens the
real project graph.

To run one module:

```powershell
python -m unittest tests.test_manifest -v
python -m unittest tests.test_acceptance_testing19 -v
python -m unittest tests.test_mcp_protocol -v
python -m unittest tests.test_mcp_tools -v
python -m unittest tests.test_mcp_safety -v
python -m unittest tests.test_freshness -v
```

---

## What M1 does

1. **Project configuration** — TOML, validated, and required to be secret-free.
2. **Windows path normalization** — a `display` form for humans and a
   lower-cased forward-slash `key` form for every comparison.
3. **Include-root containment** — every path is checked literally *and* after
   full resolution; a shared prefix (`testing19-evil`) is not containment.
4. **Symlink protection** — symlinks and directory junctions are detected and
   never traversed.
5. **Exclusion rules** — applied during traversal, so excluded directories are
   never entered.
6. **Git and working-tree baseline** — branch, commit, dirty state, and a
   per-file clean/modified/untracked/ignored state.
7. **SQLite schema** — versioned, deterministic keys, no file contents.
8. **Manifest parser** — `ast.literal_eval` only; repository code never runs.
9. **Module nodes**.
10. **`depends_on` relations** with manifest file and line provenance.

## What M2 does

A local **MCP server over stdio**, implemented directly against JSON-RPC 2.0.
It supports `initialize`, `notifications/initialized`, `ping`, `tools/list`,
and `tools/call`.

**One protocol revision is advertised: `2025-06-18`.** Responses use that
revision's shapes — `structuredContent` on tool results, `title` on tool
descriptors — and conformance with the 2025-03-26 and 2024-11-05 schemas has
not been tested. Echoing an older version back would claim compatibility this
server has not demonstrated, so any other well-formed version string is
answered with `2025-06-18` and the client decides whether it can proceed.

| Tool | Answers |
| --- | --- |
| `kg_status` | Which project, schema and indexer version, indexed commit, current commit, and **fresh / stale_commit / stale_worktree / unknown** |
| `kg_stats` | A fixed-size summary: counts, origins, bindings, off-series modules, most-referenced dependencies |
| `kg_find_modules` | Substring search with filters, paginated, hard limit 100 |
| `kg_get_module` | One module's manifest data, classification, and a purpose composed strictly from stored fields |
| `kg_dependencies` | What a module needs, direct or transitive, external targets marked and never expanded |
| `kg_dependents` | Which project modules need it, direct or transitive |
| `kg_impact` | Bounded change impact from manifest `depends_on` edges, with its evidence limits stated |

### Limits, enforced not suggested

| Bound | Value |
| --- | --- |
| Results per `kg_find_modules` call | 20 default, **100 maximum** |
| Traversal depth | 1 default, **5 maximum** |
| Nodes per traversal | **200**, regardless of depth |
| Search string | 100 characters |

Exceeding a bound is **refused with an actionable message**, not silently
truncated. When a walk stops early — depth budget spent or node cap reached —
the response says so in both the text and `truncated_reason`, so a partial
answer can never be mistaken for a complete one.

### M2 safety model

* The graph is opened through a `file:...?mode=ro` URI **and** `PRAGMA
  query_only = ON`. Two independent locks, both tested.
* **No arbitrary SQL.** Every statement is a literal in `mcp/queries.py`;
  caller input reaches SQL only through `?` placeholders. `LIKE` patterns are
  escaped, so `%` in a search is a literal percent sign.
* **No path arguments.** No tool accepts a file, directory, or database path.
  The database location comes only from the config file the server was started
  with. A test asserts no tool schema ever grows such an argument.
* stdout carries protocol only. `sys.stdout` is redirected to stderr at
  startup, so a stray `print()` anywhere in the process cannot corrupt the
  stream.
* A stale graph prefixes **every** tool response with a warning rather than
  answering confidently from an out-of-date index.
* Tool failures are returned as `isError` results, so the model can see and
  correct them; only protocol violations become JSON-RPC errors.

### Response shape

A successful `tools/call` returns exactly one `TextContent` block **and**
`structuredContent`. The text block holds the serialized JSON of the same
object, so a client that reads only text loses nothing. The human-readable
rendering travels inside that object as `summary` rather than replacing it, and
a stale-graph warning is prefixed to `summary` as well as reported in
`graph_freshness`. Error results are plain prose, not JSON — there the message
*is* the payload.

### Lifecycle

`initialize` must be the first request. Anything else beforehand is refused
with JSON-RPC error `-32002`; only `ping` and notifications are accepted
earlier. Notifications never receive a response — not a result, and not an
error either, even when dispatching one fails.

The handshake is validated strictly, because everything after it depends on
having agreed a version with a client that identified itself. `params` must be
an object carrying a non-empty string `protocolVersion`, an object
`capabilities`, and a `clientInfo` object with string `name` and `version`.
Anything missing or mistyped is refused with `-32602` and leaves the session
closed. An `initialize` sent without an id is silently ignored and does **not**
start the session: a handshake that cannot be answered has not happened.

## What M1 deliberately does not do

No Python model analysis · no XML analysis · no CSV/security analysis · no
JavaScript or OWL · no Odoo core indexing · no AI-generated nodes · no
PostgreSQL connection · no network calls · no dependency installs.

## What M2 deliberately does not do

No writes of any kind · no indexing · no PostgreSQL · no network · no deep
Python/XML/JS/security analysis · no AI-generated business analysis. The
`purpose` field is composed from stored manifest fields and is never inferred;
M1 does not index a manifest's `summary`, `description`, or `category`, so no
richer purpose is available without extending the M1 schema.

`kg_impact` sees manifest dependencies only. A module that inherits a model,
overrides a view, or calls a method **without** declaring a manifest dependency
does not appear in an impact result. The tool states this in every response
rather than presenting manifest coverage as full coverage.

---

## Safety model

**Read**: only `repo_root`, only through the scanner, only what the exclusion
rules permit. `core_root` is recorded in the graph's project metadata but is
**not** an include root in M1 — no Odoo core file is opened.

**Write**: exactly one location, enforced at runtime by
`odoo_kg/safety.py` before every write:

```
%LOCALAPPDATA%\odoo-kg\projects\sample-project\graph.db
```

The write guard refuses any target that is inside `repo_root`, inside
`core_root`, inside any `.git` or `.claude` directory, outside
`%LOCALAPPDATA%\odoo-kg\projects`, or outside the configured `data_dir`.

Source files are opened through a single choke point that hard-codes binary
read mode, so no code path can open an indexed file for writing.

### Exclusions

Pruned before traversal: `.claude`, `.git`, `venv`, `.venv`, `env`,
`__pycache__`, `node_modules`, `filestore`, `data_dir`, `sessions`, and
`static/lib` at any depth.

Excluded by name: `.env*`, `*.conf`, `*.cfg`, `*.ini`, `*.key`, `*.pem`,
`*.p12`, `*.pfx`, `*.crt`, `*.cer`, `credentials*`, `secrets*`, `*.sql`,
`*.dump`, `*.bak`, `*.zip`, `*.tar.gz`, `*.tgz`, `*.tar`, `*.gz`, `*.bz2`,
`*.xz`, `*.7z`, `*.rar`, `*.log`, `*.po`, `*.pot`, `*.mo`, plus binaries,
images, audio, video, fonts, office documents and database files.

Configuration may **add** exclusions but never remove one. `remove_dir_names`,
`remove_file_globs` and `disable_builtin` are rejected as configuration errors.

---

## Data model

**Module** — `technical_name`, `manifest_name`, `version`,
`version_series_ok`, `author`, `license`, `origin`, `origin_rule`,
`present_in_source`, `installable` (+ `installable_declared`),
`installed_state` (always `unknown` in M1), `application`, `auto_install`
(+ raw declaration), `modified_worktree`, `manifest_file`, `manifest_line`,
`module_dir`, `content_state`.

**File** — `file_key` (normalized), `display_path`, `rel_path`, `content_sha`
(sha256), `git_blob_sha` (git's own sha1 object id), `index_blob_sha`,
`content_state`, `hash_state`, `base_commit`, `size_bytes`.

**Project** — `project_id`, `repo_root`, `core_root`, `branch`, `base_commit`,
`dirty`, `indexed_at`, `schema_version`, `indexer_version`, `milestone`.

**depends_on** — `src_module`, `dst_module` (null when unresolved),
`dst_unresolved`, `binding`, `src_manifest_file`, `src_manifest_line`,
`provenance`.

### Rules that the schema enforces

- `dst_unresolved` always keeps the **original** target name, including after
  `dst_module` is resolved. Provenance survives resolution.
- An unresolved dependency never creates a Module node. A project that depends
  on core `sale` does not gain a phantom `sale` module.
- Node and edge primary keys are derived from stable identity, so two identical
  runs update in place. Rows not touched by a run are pruned afterwards.
- `graph_digest()` hashes every stable column, excluding per-run bookkeeping.
  That is how idempotency is verified rather than assumed.
- Indexing reads the current working copy, including saved but uncommitted
  files. No file contents are stored in the graph.

### Module origin

`origin` is `unknown` unless an explicit rule claims the module. This is
deliberate: an unclaimed module is an open question, not a project module.

Configured explicitly for this project:

| Module | Origin |
| --- | --- |
| `base_accounting_kit` | `vendor` |
| `base_account_budget` | `vendor` |
| `dynamic_accounts_report` | `vendor` |
| `website_helpdesk_support_ticket` | `vendor` |
| `membership` | `core-vendored` |

To claim a family of modules, add a glob rule to `[[origin.rules]]` in the
config. None is enabled by default, so no module currently carries
`origin = "project"`.

---

## Layout

```
c:\odoo\tools\odoo-kg\
├─ odoo_kg\
│  ├─ __init__.py       version, schema version, milestone
│  ├─ __main__.py       python -m odoo_kg
│  ├─ cli.py            doctor / index / stats / verify / serve
│  ├─ config.py         TOML loading, validation, secrets guard
│  ├─ errors.py         exception hierarchy
│  ├─ exclusions.py     mandatory + additive exclusion rules
│  ├─ freshness.py      M2: is the graph still current?
│  ├─ gitbaseline.py    read-only git interrogation
│  ├─ indexer.py        the M1 pipeline
│  ├─ manifest.py       ast.literal_eval manifest parsing
│  ├─ safety.py         write guard, read-only file access
│  ├─ scanner.py        traversal, hashing, containment
│  ├─ store.py          SQLite schema and upserts
│  ├─ verify.py         acceptance criteria as data
│  ├─ winpath.py        Windows path normalization and guards
│  └─ mcp\              M2: the MCP server
│     ├─ __init__.py    server name, supported protocol versions
│     ├─ jsonrpc.py     JSON-RPC 2.0 framing and error codes
│     ├─ limits.py      every hard cap, in one place
│     ├─ queries.py     all SQL: static, parameterised, bounded
│     ├─ readonly_db.py mode=ro + query_only connection
│     ├─ server.py      MCP dispatch and the stdio loop
│     └─ tools.py       tool schemas, validation, handlers
├─ config\
│  └─ sample-project.toml
├─ tests\
└─ pyproject.toml       metadata only; zero dependencies
```
