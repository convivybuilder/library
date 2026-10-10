#!/bin/sh
# Start the Library, refusing to serve on an interpreter that cannot do the job
# or that somebody outside this machine's administrators could have tampered
# with.
#
# WHY THE PREFLIGHT EXISTS. On 2026-08-13 this service had been running since the
# previous afternoon under the SYSTEM python3.14 rather than the repo's `.venv`,
# because it was hand-started and nothing said which interpreter to use. That
# interpreter has no `fastembed`. `vectors._fastembed` imports the model lazily —
# deliberately, so the deterministic routes never wait on an ONNX runtime — so
# every route worked and only `GET /signals/search` failed, and it failed with a
# 503 nobody was reading. It stayed broken for a day and was found by the shadow
# harness, which is the one thing looking.
#
# So: pin the interpreter, and check its imports BEFORE binding the port. The
# import is the cheap half (the model is constructed on first use, not on
# import), so this costs a fraction of a second and converts a silent one-route
# outage into a service that will not start.
#
# WHY THE GUARD WALKS THE WHOLE PATH. The ownership guard below was written wrong
# four times, and every time the same way: it judged SOME of what decides which
# code runs, and the part it left out was the part that mattered. It read one
# permission digit; then it stat'd a symlinked `.venv` instead of its target;
# then it stat'd the venv root while `exec`ing a `bin/python` that could point
# anywhere; then it checked the resolved target's directory, which on this
# deployment is Homebrew's, and so never looked at `.venv/bin` — the directory
# that actually holds the `python` symlink. Every one of those was found by a
# reviewer building the case, never by anybody reading the file.
#
# There is no partial version of this check that is worth having, so this one
# takes the whole path: every directory traversed, every symlink followed, and
# the file at the end, each judged in its own right.
set -eu

die() {
    echo "serve: $1" >&2
    exit 78   # EX_CONFIG
}

# --- the guard ------------------------------------------------------------------

# Which of two forms to run below. `stat -f` and `ls -lde` are BSD tools with
# no such flags on Linux; GNU `stat -c` and `getfacl` are what this checks
# instead there. Read once, so 64 path components do not each re-run `uname`,
# and refused outright on anything neither branch below knows how to read —
# a guard that guessed on an unrecognised platform would be exactly the kind
# of partial coverage that got it wrong five times already.
UNAME_S="$(uname -s)" || die "cannot determine the platform (uname -s failed) — refusing"
case "$UNAME_S" in
    Darwin|Linux) ;;
    *) die "this guard only knows Darwin and Linux, not '$UNAME_S' (uname -s) — refusing, because a platform it was not built to read is a platform it cannot vouch for" ;;
esac

# The only owners that may appear on the path to the interpreter. A component
# owned by anybody else is code somebody else controls.
ME="$(id -u)"

# Groups whose members already administer this machine, so a group-writable
# component grants them nothing they do not have anyway. On macOS those are
# `wheel` (0) and `admin` (80), the group macOS grants local administrator rights
# through; on the deployment machine `admin` resolves to root, _mbsetupuser and
# this user.
#
# THIS CARVE-OUT IS FORCED, not a convenience. The interpreter is reached through
# Homebrew's prefix, and Homebrew installs that prefix group-writable by `admin`:
# measured on the deployment machine, `/opt/homebrew/bin` and
# `/opt/homebrew/Cellar` are both mode 775 group admin, and the real chain runs
# through Cellar. Refusing group-writable outright would refuse to start on a
# stock Homebrew install — the same class of bug as the versions that checked too
# little, pointed the other way, and it would take the service down rather than
# leave a hole. What is left is the honest residual: somebody who can write
# Homebrew's prefix is a local administrator of this machine already.
#
# LINUX HAS NO EQUIVALENT GROUP. gid 80 is `admin` only by macOS convention;
# on Linux that number names whatever group this machine happened to assign
# it, and nobody here knows who its members are. Carrying the same carve-out
# over would widen the guard rather than merely dodge a stock-install false
# refusal, so Linux keeps only root's gid.
case "$UNAME_S" in
    Darwin) ADMIN_GIDS=" 0 80 " ;;
    Linux)  ADMIN_GIDS=" 0 " ;;
esac

# Refuse one component of the path.
#
# lstat, never stat: the walk below follows symlinks itself and checks each
# target in its own right, so what this has to judge is the link, not the thing
# the link points at.
#
# It fails CLOSED. A `stat` that errors means we could not determine whether this
# component is safe, and "could not determine" is not "safe" — the version before
# this one read a file that vanished mid-check as permissions 000 and passed it,
# which was the one path in the file that went that direction.
refuse_component() {
    _path="$1"

    case "$UNAME_S" in
        Darwin) _meta=$(stat -f '%u %g %Lp' "$_path" 2>/dev/null) ;;
        Linux)  _meta=$(stat -c '%u %g %a' "$_path" 2>/dev/null) ;;
    esac || die "$WHAT: cannot stat $_path — refusing, because a component we cannot read is a component we cannot vouch for"

    # Three fields or nothing. `stat` returning something this cannot parse is an
    # unreadable component, and unreadable is a refusal like every other
    # inconclusive read here.
    case "$_meta" in
        *" "*" "*) ;;
        *) die "$WHAT: cannot read the ownership of $_path (stat returned '$_meta') — refusing" ;;
    esac

    _owner=${_meta%% *}
    _tailmeta=${_meta#* }
    _gid=${_tailmeta%% *}

    # Zero-padded to three digits so group and other sit at fixed offsets. `%Lp`
    # prints the permission bits as unpadded octal — 755 as "755", 040 as "40",
    # 0 as "0" — and reading a fixed position off an unpadded string is how the
    # first version of this check inspected the wrong digit.
    #
    # PAD AS A STRING, and refuse anything that is not three octal digits after
    # padding. `printf '%03d'` looks like the obvious way to do this and fails
    # open twice: it reads a leading zero as octal, turning "070" into "056", and
    # it passes a four-digit value through unchanged, after which `${_perms#??}`
    # yields "55" rather than a digit and matches none of the patterns below. Both
    # would let the component past. `%Lp` is masked to the permission bits, so
    # setgid, setuid and sticky are already gone and neither case should arise —
    # but every previous version of this guard was undone by an input it assumed
    # could not arrive, so an unreadable mode is a refusal rather than a pass.
    # Empty before padding, not after: the pad loop would turn "" into "000",
    # which is three valid octal digits granting nobody anything, so a mode that
    # came back empty would sail through the check below as the safest possible
    # value. Refuse it while it is still recognisable as missing.
    _perms=${_tailmeta##* }
    [ -n "$_perms" ] || die "$WHAT: cannot read the mode of $_path (stat returned '$_meta') — refusing"
    while [ ${#_perms} -lt 3 ]; do _perms="0$_perms"; done
    case "$_perms" in
        [0-7][0-7][0-7]) ;;
        *) die "$WHAT: cannot read the mode of $_path ('$_perms' is not three octal digits) — refusing" ;;
    esac

    # GNU `%a`, UNLIKE BSD `%Lp`, PRINTS A FOURTH DIGIT WHEN SETUID, SETGID OR
    # STICKY IS SET — measured by chmod'ing each bit in turn under Linux and
    # reading "4755", "2755" and "1755" back, where `%Lp` would have printed
    # "755" for all three. Nothing above strips that leading digit: a
    # four-character `_perms` fails the three-octal-digit pattern above and
    # refuses through the same path as any other unreadable mode, which is
    # the "otherwise refuse" this guard has taken on every input it did not
    # expect since the version that assumed setuid/setgid/sticky could not
    # arrive here. Stripping the leading digit to recover the low three would
    # need proving that bit carries no meaning for what this function decides,
    # which nothing here has done, so it stays a refusal rather than a guess.

    # For "755": `${_perms#??}` is "5", the other digit; `${_perms#?}` is "55"
    # and trimming its last character gives "5", the group digit.
    _other=${_perms#??}
    _group=${_perms#?}
    _group=${_group%?}

    if [ "$_owner" != "$ME" ] && [ "$_owner" != 0 ]; then
        die "$WHAT: $_path is owned by uid $_owner, not $ME or root — refusing"
    fi

    # ON LINUX, EVERY SYMLINK LSTATS AS MODE 777, and the kernel enforces
    # nothing from it: dereferencing a symlink is governed entirely by the
    # TARGET's own permissions, and replacing the link is governed entirely
    # by whoever can write the DIRECTORY that holds it — which this walk
    # already checks in its own right, as the directory's own path component.
    # A straight port of the mode and ACL checks below would therefore read
    # every symlink's own manufactured 777 as world-writable and refuse the
    # whole chain, on every Linux deployment, regardless of what actually
    # guards it. So: on Linux, a component that is itself a symlink skips the
    # mode and ACL checks below and keeps only the owner check just above.
    # macOS symlink modes are real numbers subject to real umask and chmod,
    # so nothing changes there.
    if [ "$UNAME_S" = Linux ] && [ -L "$_path" ]; then
        return 0
    fi

    # A sticky world-writable directory is not carved out, so anything reached
    # through /tmp is refused (`%Lp` masks the sticky bit off, so /private/tmp
    # reads here as plain 777). Sticky does make substitution harder, but no
    # deployment path runs through such a directory and a venv that did would
    # deserve the refusal. Worth knowing before building a test tree under /tmp
    # and reading the refusal as a bug.
    case "$_other" in
        [2367]) die "$WHAT: $_path is world-writable (mode $_perms) — refusing, because anything that can write it can plant code in the process that will hold the only copy of the vaults" ;;
    esac

    case "$_group" in
        [2367])
            case "$ADMIN_GIDS" in
                *" $_gid "*) ;;
                *) die "$WHAT: $_path is writable by gid $_gid (mode $_perms), which is not an administrator group — refusing" ;;
            esac ;;
    esac

    # THE MODE BITS ARE NOT THE WHOLE ANSWER ON EITHER FILESYSTEM. Both macOS
    # and Linux grant access through ACLs as well, independently of the bits
    # read above, and each platform is checked in its own right below.
    case "$UNAME_S" in
    Darwin)
        # macOS's ACLs are invisible to `%Lp`: a directory reads 755 here while
        # an ACL entry lets every local account add and delete files in it.
        # Confirmed by building one — a directory left at mode 755 with
        # `chmod +a "everyone allow write,delete, add_file,delete_child"`
        # still stats as 755, and the checks above pass it. That is the
        # round-4 attack again, a writable directory holding the symlink,
        # reached through a mechanism none of the arithmetic above can see.
        #
        # An ALLOW entry is refused and a DENY entry is ignored, because a deny
        # can only take access away. Refusing every ACL outright would be
        # simpler and is wrong: a home directory carries `group:everyone deny
        # delete` as macOS ships it, so a blanket rule would refuse to start on
        # a stock home directory. There is no allow entry anywhere on the
        # deployment chain, measured component by component, so this costs
        # nothing today and refuses if one ever appears.
        #
        # No attempt is made to decide WHICH principal an allow entry names or
        # whether its verbs are write-shaped. Parsing that is exactly the kind
        # of partial cleverness that got this guard wrong four times; an ACL
        # on the path to the interpreter is unusual enough that refusing with
        # a message naming the path is the better failure.
        _acl=$(ls -lde "$_path" 2>/dev/null) \
            || die "$WHAT: cannot read the ACL of $_path — refusing, because a component whose ACL we cannot read is a component we cannot vouch for"
        if printf '%s\n' "$_acl" | grep -qE '^[[:space:]]*[0-9]+:.* allow '; then
            die "$WHAT: $_path carries an ACL granting access its mode ($_perms) does not show — refusing; inspect it with: ls -lde $_path"
        fi
        ;;
    Linux)
        # `ls -lde` on Linux prints a plain, ACL-less listing whatever the
        # ACL actually holds, because it never really read one — the same
        # shape as the round-4 attack this guard exists to close, reached
        # through a command that stopped erroring instead of a command that
        # never checked. `getfacl` is the tool that actually reads POSIX ACLs.
        #
        # THE POSITIVE CONTROL BELOW EXISTS FOR EXACTLY THAT REASON: a
        # `getfacl` that runs, exits 0, and prints something this guard does
        # not recognise (empty output, a stub, a future format change) must
        # refuse rather than pass by default, because empty or unrecognised
        # output is not evidence there is no ACL — it is evidence this guard
        # could not read one. Requiring the three base entries `user::`,
        # `group::` and `other::` to be present is what makes "getfacl says
        # nothing is wrong" and "getfacl said nothing" distinguishable.
        #
        # A NAMED ENTRY IS REFUSED — `user:<name>:` or `group:<name>:`, and
        # the `default:` form of either, which governs what a new file created
        # here inherits — for the same reason macOS's ALLOW entries are: none
        # exist anywhere on a clean chain, so this costs nothing today and
        # refuses the moment one grants access the mode bits do not show. A
        # bare `mask::` line is not a grant to any principal by itself — it
        # only caps what named entries are allowed, and none are allowed here
        # — so it does not refuse on its own.
        command -v getfacl >/dev/null 2>&1 \
            || die "$WHAT: getfacl is not on PATH — refusing, because $_path's ACL cannot be vouched for without it; install the 'acl' package"
        _facl=$(getfacl --absolute-names --omit-header "$_path" 2>/dev/null) \
            || die "$WHAT: cannot read the ACL of $_path with getfacl — refusing, because a component whose ACL we cannot read is a component we cannot vouch for"
        case "$_facl" in
            *"user::"*) ;;
            *) die "$WHAT: getfacl on $_path did not report the expected 'user::' base entry — refusing, because output this does not recognise is output it cannot vouch for" ;;
        esac
        case "$_facl" in
            *"group::"*) ;;
            *) die "$WHAT: getfacl on $_path did not report the expected 'group::' base entry — refusing, because output this does not recognise is output it cannot vouch for" ;;
        esac
        case "$_facl" in
            *"other::"*) ;;
            *) die "$WHAT: getfacl on $_path did not report the expected 'other::' base entry — refusing, because output this does not recognise is output it cannot vouch for" ;;
        esac
        # `grep`, not a `case` inside a loop inside `$( )`: bash invoked as
        # `sh` — what this script runs under, per the WHY comment on
        # `check_worktree_clean` below — cannot parse a `case` pattern's
        # closing paren inside a command substitution at all, mistaking it
        # for the substitution's own close and failing with a syntax error
        # on the `;;` that follows; confirmed by reproducing it with exactly
        # this shape. `grep -v` sidesteps the parser entirely, and its own
        # "nothing matched" exit status (1, on the clean and expected case
        # where every line is one of the base entries) is absorbed by
        # `|| true` so it does not itself trip `set -e`.
        _bad_acl_line=$(printf '%s\n' "$_facl" \
            | grep -vE '^$|^(default:)?(user|group|other|mask)::' || true)
        [ -z "$_bad_acl_line" ] \
            || die "$WHAT: $_path carries an ACL entry beyond its base permissions ($_bad_acl_line) — refusing; inspect it with: getfacl --absolute-names $_path"
        ;;
    esac
}

# Resolve $1 to what it actually is, refusing at the first component anybody
# outside {this user, root, this machine's administrators} could modify. Prints
# the resolved path.
#
# EVERY component, because every one of them decides what runs: a directory
# anybody can write is a directory whose entries they can replace, whether the
# entry is the interpreter itself or a symlink two hops before it.
#
# CHECKING EVERY COMPONENT IS ALSO WHAT MAKES THE LATER `exec` SAFE. This walk
# resolves the chain once and the kernel resolves it again at `exec`, which is a
# TOCTOU wherever the two could come to different answers. Here they cannot:
# once every component is known to be writable only by this user, root, or an
# administrator, nobody who is not already one of those can change what the path
# resolves to in between. The window closes because of who can reach into it,
# not because it is narrow.
#
# The step budget bounds a symlink cycle, which under `KeepAlive` would spin this
# at every boot rather than fail, and a pathological depth with it. The real
# chain on the deployment machine takes 24 steps, counted rather than estimated.
walk_checked() {
    WHAT="$2"
    _steps=0
    _done=""            # what is checked and resolved so far; "" is /
    _rest="$1"

    case "$_rest" in
        /*) ;;
        *)  _rest="$(pwd -P)/$_rest" ;;
    esac
    _rest=${_rest#/}

    refuse_component /

    while [ -n "$_rest" ]; do
        # Counted up rather than down, so the bound in the message is the bound in
        # the code: 64 steps are allowed and the 65th refuses.
        _steps=$((_steps + 1))
        [ "$_steps" -le 64 ] \
            || die "$WHAT: more than 64 components and symlink hops resolving $1 — refusing, because this is a cycle or a depth nothing real has"

        case "$_rest" in
            */*) _head=${_rest%%/*}; _next=${_rest#*/} ;;
            *)   _head=$_rest;       _next="" ;;
        esac

        case "$_head" in
            ""|.) _rest=$_next; continue ;;
            ..)   _done=${_done%/*}; _rest=$_next; continue ;;
        esac

        _cand="$_done/$_head"
        refuse_component "$_cand"

        if [ -L "$_cand" ]; then
            _target=$(readlink "$_cand") \
                || die "$WHAT: cannot read the symlink at $_cand — refusing"
            case "$_target" in
                # An absolute target restarts at the root; a relative one is
                # resolved against the directory holding the link, which is
                # `$_done` and stays where it is.
                /*) _done=""; _rest=${_target#/} ;;
                *)  _rest=$_target ;;
            esac
            [ -z "$_next" ] || _rest="$_rest/$_next"
        else
            _done="$_cand"
            _rest=$_next
        fi
    done

    [ -n "$_done" ] || _done=/
    printf '%s\n' "$_done"
}

# --- where we are and what we run -----------------------------------------------

# RESOLVED, not merely absolute. `cd "$(dirname "$0")" && pwd` gives an absolute
# path but keeps a symlinked component, so invoking this through a convenience
# symlink picks up the OTHER checkout's venv and serves its code — the same
# wrong-checkout hazard the hardcoded path had, which is what this replaced.
# launchd invokes the real path either way; a human does not. The script is code
# that runs, so its own path is walked on the same terms as the interpreter's.
#
# IN TWO STEPS, and never nested inside `dirname`. `die` is an `exit`, and an
# `exit` inside `$( )` leaves only the subshell, so the refusal reaches the script
# as the substitution's exit status and nothing more. Wrap that substitution in
# another one and the wrapper's own status is what survives: `dirname ""` prints
# "." and succeeds, so a guard that refused would have carried on with DIR set to
# the working directory. Each substitution is checked where its status is still
# the one being read.
SELF="$(walk_checked "$0" "this script")" || exit 78
DIR="$(dirname "$SELF")"

# Overridable, so the refusal paths can be exercised without editing this file.
PY="${PY:-$DIR/.venv/bin/python}"

# `--check` runs everything the real start runs except the `exec`: the path walk,
# the interpreter check, the import preflight, and the second walk. It answers
# "is this venv safe and complete enough to serve from" without starting a
# service, which is a question worth asking before the vault cutover and after
# anything touches the venv.
#
# It is also what the tests drive, and that is not incidental. A test that asks
# whether the REAL interpreter passes the guard has to point at the real venv,
# and without this it would go on to import the app, read the live credentials
# and the live control DB, and bind port 8100 — serving real requests for as long
# as the test let it. That is only invisible when something already holds the
# port, which is exactly the sort of ambient assumption a fresh clone breaks.
#
# `--walk PATH` runs the same ownership/mode/ACL walk above against an
# arbitrary path and nothing else — no interpreter, no import preflight, no
# branch check. `mtls.py`'s TLS preflight is the caller: the mutual-TLS
# listener's trust anchor needs the identical owner/group/mode/ACL guarantee
# this file's interpreter chain already gets, and this guard was written
# wrong four times by partial reimplementations — a second walk in Python
# would risk becoming the fifth. Reusing this one instead means a fix here
# fixes both callers at once, and there is exactly one walk to keep correct.
CHECK_ONLY=0
WALK_ONLY=0
WALK_TARGET=""
case "${1:-}" in
    --check)
        [ "$#" -eq 1 ] \
            || { echo "serve: too many arguments — --check takes none" >&2; exit 64; }
        CHECK_ONLY=1 ;;
    --walk)
        [ "$#" -eq 2 ] \
            || { echo "serve: --walk takes exactly one path argument" >&2; exit 64; }
        WALK_ONLY=1
        WALK_TARGET=$2 ;;
    "")
        [ "$#" -eq 0 ] \
            || { echo "serve: too many arguments — the only options are --check and --walk PATH" >&2; exit 64; }
        ;;
    *)
        echo "serve: unknown argument '$1' — the only options are --check and --walk PATH" >&2
        exit 64 ;;   # EX_USAGE
esac

# Independent of DIR, PY, the interpreter, and the branch guard — this walks
# only the path the caller named and then exits, before any of that other
# state is touched.
if [ "$WALK_ONLY" = 1 ]; then
    walk_checked "$WALK_TARGET" "the walked path"
    exit 0
fi

cd "$DIR"

if [ ! -x "$PY" ]; then
    echo "serve: no interpreter at $PY — create it with:" >&2
    echo "  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
    exit 78
fi

# RESOLVE TO CHECK, EXECUTE THE ORIGINAL. These are different paths and an
# earlier version conflated them, which broke the service outright:
# `.venv/bin/python` is a symlink to the Homebrew interpreter, and a venv works
# precisely BECAUSE python is invoked through the venv path — `sys.prefix` comes
# from argv[0]. Exec the resolved target and the venv silently disappears, so
# `fastembed` is missing and the preflight refuses. The security question is
# "what code is really there", which needs the walk; the execution question is
# "which environment", which needs the original.
PY_REAL="$(walk_checked "$PY" "the interpreter")" || exit 78
echo "serve: $PY resolves to $PY_REAL, whole path checked"

# --- the imports ----------------------------------------------------------------

# Every import the service needs at RUNTIME, including the lazy one. `fastembed`
# is named explicitly because it is the one a route defers, so it is the one an
# ordinary start cannot notice is missing.
"$PY" - <<'PREFLIGHT' || exit 78
import sys
missing = []
for mod in ("fastapi", "uvicorn", "sqlite_vec", "fastembed"):
    try:
        __import__(mod)
    except ImportError as e:
        missing.append(f"{mod} ({e})")
if missing:
    print("serve: preflight FAILED, refusing to start on this interpreter:",
          file=sys.stderr)
    for m in missing:
        print(f"  missing: {m}", file=sys.stderr)
    print(f"  interpreter: {sys.executable}", file=sys.stderr)
    print("  fix: .venv/bin/pip install -r requirements.txt", file=sys.stderr)
    sys.exit(1)
print(f"serve: preflight ok on {sys.executable}")
PREFLIGHT

# --- the app's own import-time configuration refusals ---------------------
#
# WHY THIS EXISTS. On 2026-10-01 `bin/install-library` installed cd9693a
# after the preflight above passed, and every start then died at `app.py`
# import: `store.resolve_cold_db_path()` raised `MisconfiguredColdDB`
# because the service plist lacked `LIBRARY_COLD_DB`. The preflight above
# imports `fastapi`/`uvicorn`/`sqlite_vec`/`fastembed` and never `app`
# itself, so an import-time configuration refusal inside `app.py` — or
# anything it imports: `store.resolve_db`, `vectors.resolve_vectors_db_path`,
# `blobs.resolve_blob_dir` — got past `--check` every time. The Library was
# down from 16:28Z to 16:31Z. This imports `app` for real, so the next
# missing variable fails the install instead of the service.
#
# UNDER THE PLIST'S OWN ENVIRONMENT, NEVER THIS PROCESS'S. The installer's
# own shell is not what runs the service — the plist `launchd` loads is —
# so judging the import on the installer's exported variables would pass or
# fail on the wrong environment entirely. `LIBRARY_CHECK_PLIST` names the
# plist to read; `bin/install-library` sets it to the staged candidate
# before the swap. Unset — an ordinary `serve.sh --check` with no plist to
# check against — this step is skipped rather than guessing at an
# environment nobody named.
if [ "$CHECK_ONLY" = 1 ] && [ -n "${LIBRARY_CHECK_PLIST:-}" ]; then
    [ -f "$LIBRARY_CHECK_PLIST" ] \
        || die "LIBRARY_CHECK_PLIST names $LIBRARY_CHECK_PLIST, which does not exist"

    # Two checks below, each naming what it catches. This grep catches a
    # plist with no literal <key>EnvironmentVariables</key> text, which
    # covers both a plist with no such dict and a binary-format plist
    # (plutil can convert a hand-edited launchd plist to one), since binary
    # plists carry no XML tags. The zero-pairs check after the parser catches
    # an XML plist whose dict is present but yields no pairs, such as an empty
    # <dict></dict>. The awk parser itself exits 0 either way, so there is no
    # success signal distinct from "found nothing" without these.
    # Die here, naming the plist, rather than importing app under an
    # environment emptier than what this plist actually sets; relying on
    # every module app.py imports to keep having no default for its own
    # required variable is not a check, it is luck.
    #
    # KNOWN LIMIT, NOT CAUGHT BY EITHER CHECK: the parser is line-oriented —
    # a <string> value split across multiple lines (the opening tag on its
    # own line, the text on the next) truncates to an empty value rather than
    # the zero pairs these checks watch for, because the regex that extracts
    # the value runs against the line holding <string> only. A self-closing
    # <dict/> for EnvironmentVariables is the same gap from the other side:
    # the close pattern below is /<\/dict>/, which a self-closing tag never
    # matches, so in_env never turns back off and the plist's own keys AFTER
    # EnvironmentVariables get read as environment pairs too, not just the
    # ones inside it. No plist the Library ships does either (confirmed by
    # reading all three under launchd/ and the live plist at
    # ~/Library/LaunchAgents/com.convivy.library.plist, and by
    # grepping bin/, launchd/, serve.sh and the top-level Python for
    # plistlib or a written <dict/>: zero hits against a positive control of
    # one); if either ever did, --check would import app with the truncated
    # or the spurious extra pairs, not refuse.
    grep -qF '<key>EnvironmentVariables</key>' "$LIBRARY_CHECK_PLIST" \
        || die "$LIBRARY_CHECK_PLIST has no EnvironmentVariables dict — refusing to import app under an environment this plist never names"

    # The same parser `bin/install-library`'s own `_plist_env_pairs` uses, kept
    # as a second copy rather than a shared one: the two scripts are never
    # sourced into each other — this one also runs standalone under launchd,
    # with no `bin/install-library` alongside it to source from — and the
    # parser is small enough that keeping two copies correct costs less than
    # the coupling a shared one would add.
    _plist_env_pairs() {
        awk '
          /<key>EnvironmentVariables<\/key>/ { in_env=1; next }
          in_env && /<\/dict>/ { in_env=0 }
          in_env && /<key>/ {
            k=$0; sub(/.*<key>/,"",k); sub(/<\/key>.*/,"",k); pending=k; next
          }
          in_env && pending != "" && /<string>/ {
            v=$0; sub(/.*<string>/,"",v); sub(/<\/string>.*/,"",v)
            print pending "=" v
            pending=""
          }
        ' "$1"
    }

    _plist_envfile="$(mktemp)" \
        || die "cannot create a temp file to hold $LIBRARY_CHECK_PLIST's environment — refusing"
    _plist_env_pairs "$LIBRARY_CHECK_PLIST" > "$_plist_envfile" \
        || { rm -f "$_plist_envfile"; die "cannot parse EnvironmentVariables out of $LIBRARY_CHECK_PLIST — refusing"; }

    [ -s "$_plist_envfile" ] \
        || { rm -f "$_plist_envfile"; die "$LIBRARY_CHECK_PLIST's EnvironmentVariables dict parsed to zero variables — refusing to import app under an environment this plist never names"; }

    echo "serve: importing app under $LIBRARY_CHECK_PLIST's own environment"

    # A SUBSHELL, so the `set --` below only ever touches positional
    # parameters inside it — this script's own argument handling, already
    # done above, is never at risk of being clobbered.
    #
    # `env -i`, not the inherited environment with overrides layered on top:
    # the whole point is that the staged build is judged on EXACTLY what the
    # plist sets, nothing this process happens to have (including whatever
    # LIBRARY_DB or LIBRARY_COLD_DB install-library's own shell exports).
    #
    # A SUBPROCESS, not an in-process `importlib.import_module` from this
    # file's own preflight interpreter session: `app.py`'s import chain is
    # read, every module-level read confirmed by reading the source — no
    # migration, no port bind, no write to either vault — but a subprocess
    # with a clean, plist-only environment is cheap here and makes "exactly
    # the plist's env, nothing this process already has" true by
    # construction rather than by auditing every import transitively, again,
    # the next time one of them changes.
    #
    # NO `cd` NEEDED HERE: this subshell inherits the `cd "$DIR"` already run
    # near the top of the script, before the branch guard, so the stdin
    # script's `sys.path[0]` (`''`, which Python resolves against the cwd at
    # import time) is already `$DIR` — the staged build's own directory,
    # where its `app.py` lives — regardless of what directory the caller
    # invoked `serve.sh` from.
    if ! (
        set --
        while IFS= read -r _pair; do
            [ -n "$_pair" ] || continue
            set -- "$@" "$_pair"
        done < "$_plist_envfile"
        env -i "$@" "$PY" - <<'PREFLIGHT'
import sys
try:
    import app
except ModuleNotFoundError as exc:
    if exc.name == "app":
        print(f"serve: cannot find app.py to import (sys.path={sys.path!r}): {exc}",
              file=sys.stderr)
    else:
        print(f"serve: app refused to import: {exc}", file=sys.stderr)
    sys.exit(1)
except Exception as exc:
    print(f"serve: app refused to import: {exc}", file=sys.stderr)
    sys.exit(1)
PREFLIGHT
    ); then
        rm -f "$_plist_envfile"
        die "the staged build's app refused to import under $LIBRARY_CHECK_PLIST's environment (see the refusal above) — the service would crash-loop on this plist; fix what the refusal names before installing"
    fi
    rm -f "$_plist_envfile"
    echo "serve: app imports cleanly under $LIBRARY_CHECK_PLIST's environment"
fi

# Walk it again, immediately before the exec. Nobody who could act on the chain
# was left able to after the first pass, so this is not what closes the TOCTOU —
# it costs 24 stat calls and removes the preflight, much the longest thing
# between the check and the exec, from the span either pass has to vouch for.
walk_checked "$PY" "the interpreter" >/dev/null

# --- the branch -------------------------------------------------------------

# The service must be running code that was reviewed, not whatever happened to
# be checked out when the process last started. WHY THIS EXISTS: recovering
# this service from an outage on 2026-08-23, DIR was found checked out to
# keel/chain-pull-run-state, a pull request still in review — caught only
# because a human looked at the branch before restarting. `KeepAlive` is true
# in the launchd job, so the next crash restarts serve.sh from whatever branch
# is on disk with no human in the loop at all. `agentctl spawn --pr N` puts a
# dispatched coder's worktree at this same DIR, which is what makes this a
# routine hazard rather than a rare one.
#
# THE NAME IS HARDCODED, not read from `refs/remotes/origin/HEAD`. That ref
# only exists once something has run `git remote set-head origin -a`, which
# is not guaranteed of every checkout this guard runs against; falling back
# silently when it is absent would be exactly the kind of partial check that
# got the ownership guard above wrong four times. This repository's default
# branch is `main`, so that is what is checked, plainly.
#
# NO FETCH. `origin/main` is read from this checkout's local refs, which can
# be stale — a service whose boot depends on GitHub being reachable is a
# worse failure than the one this prevents.
#
# THE COMPARISON IS "HAS ORIGIN/MAIN MERGED THIS", NOT "DOES THIS MATCH
# ORIGIN/MAIN". `merge-base --is-ancestor HEAD origin/main` tolerates a local
# `origin/main` ref that is stale in the direction that matters least: a
# checkout on `main` whose ref has not been updated since the last fetch
# still passes even once GitHub's `main` has moved further ahead, because
# HEAD remains an ancestor of wherever `origin/main` eventually is. What it
# still refuses is a `main` that has DIVERGED — commits `origin/main` does
# not have, whether from an unpushed local commit or a rebase — because
# those are exactly the unreviewed-code case this guard exists for. This
# guard's job is "reviewed", not "current": a stale-but-ancestor checkout
# serves old, already-reviewed code, which is a staleness problem tracked
# separately (the Library has no autodeploy), not a defect in this check.
#
# ONLY PLUMBING THAT CANNOT RUN REPOSITORY-SUPPLIED CODE. `git status` can
# invoke an fsmonitor hook or a smudge filter named in this checkout's own
# `.git/config`; `rev-parse`, `symbolic-ref`, and `merge-base` touch neither
# the index nor the working tree, so none of them invoke either. The same
# question this file's reviewers raised about `ruff check` picking up a
# repo-supplied `fix = true` applies here, and the answer for these three
# commands is that there is nothing repo-local config can turn them into.
#
# FAILS OPEN EXACTLY ONCE: when `$DIR/.git` does not exist. That is decided
# by looking for the entry itself, not by asking git to explain why a
# command failed — the two read identically to `git -C "$_dir" rev-parse
# --git-dir`, which exits 128 whether there is no repository at all or
# there is one it cannot read (confirmed by reproducing it: `chmod 000
# .git` gives exit 128, indistinguishable from no `.git` at all, and taking
# that alone as "no repository" let an unreadable `.git` on this exact
# directory pass the guard silently). A release tarball with no `.git` is a
# legitimate future deployment shape — `platform/specs/account-isolation-and-
# the-migration` is heading there — and this guard refusing to boot it for
# lacking version-control metadata would block the deployment it is meant to
# support: there is nothing to compare against, so that case passes through.
# Everything else fails CLOSED like the rest of this file, `.git` present
# included: a `.git` git cannot read, a detached HEAD, a missing origin
# remote, an unresolvable `origin/main`, or a diverged HEAD all refuse.
check_branch() {
    _dir="$1"

    # A pure filesystem test, run before anything below asks git to explain
    # itself — SERVE_ALLOW_BRANCH must stay able to bypass unconditionally,
    # including an unreadable `.git`, or the loud override stops being loud
    # and starts being a second guard.
    _git_entry=0
    [ -e "$_dir/.git" ] || [ -L "$_dir/.git" ] && _git_entry=1

    if [ -n "${SERVE_ALLOW_BRANCH:-}" ]; then
        _found="no git repository"
        if [ "$_git_entry" = 1 ] && command -v git >/dev/null 2>&1; then
            _found=$(git -C "$_dir" symbolic-ref --quiet --short HEAD 2>/dev/null) || _found="detached HEAD"
        fi
        echo "serve: SERVE_ALLOW_BRANCH is set — branch guard bypassed, serving from $_found" >&2
        return 0
    fi

    # Nothing to check against; see FAILS OPEN EXACTLY ONCE above.
    [ "$_git_entry" = 1 ] || return 0

    command -v git >/dev/null 2>&1 \
        || die "$_dir has a .git entry but no git executable is available to read it — refusing; set SERVE_ALLOW_BRANCH=1 to bypass"
    git -C "$_dir" rev-parse --git-dir >/dev/null 2>&1 \
        || die "$_dir has a .git entry that git cannot read — refusing to guess whether this is reviewed code; set SERVE_ALLOW_BRANCH=1 to bypass"

    _branch=$(git -C "$_dir" symbolic-ref --quiet --short HEAD 2>/dev/null) \
        || die "$_dir has a detached HEAD, not the main branch — refusing to serve unreviewed code; set SERVE_ALLOW_BRANCH=1 to bypass"
    [ "$_branch" = "main" ] \
        || die "$_dir is on branch '$_branch', not main — refusing to serve unreviewed code; set SERVE_ALLOW_BRANCH=1 to bypass"

    _head=$(git -C "$_dir" rev-parse HEAD 2>/dev/null) \
        || die "cannot resolve HEAD at $_dir — refusing; set SERVE_ALLOW_BRANCH=1 to bypass"
    _origin_main=$(git -C "$_dir" rev-parse origin/main 2>/dev/null) \
        || die "cannot resolve origin/main at $_dir (no origin remote, or origin/main not known to this local checkout) — refusing; set SERVE_ALLOW_BRANCH=1 to bypass"

    git -C "$_dir" merge-base --is-ancestor "$_head" "$_origin_main" \
        || die "$_dir's HEAD ($_head) is not an ancestor of origin/main ($_origin_main), checked locally without fetching — refusing to serve code origin/main has not reviewed; set SERVE_ALLOW_BRANCH=1 to bypass"

    check_worktree_clean "$_dir" "$_head"
    check_untracked_shadow "$_dir"
}

# The working tree must match what HEAD says is there. HEAD being an
# ancestor of `origin/main` says the last COMMIT was reviewed; it says
# nothing about what is actually on disk. `serve.sh` execs uvicorn with
# `$DIR` as its working directory, so disk content is what gets served, not
# the commit the checks above compared. A stray hand-edit to `app.py`, a
# debugging change left uncommitted, an interrupted merge, a leftover edit
# from whoever last had a shell open in this directory: all boot silently
# under a check that only compares refs. An uncommitted edit is the most
# unreviewed code there is.
#
# NEITHER `git status` NOR `git diff` IS USED HERE, for the same reason
# `rev-parse` and `symbolic-ref` were chosen above and no further: both
# compare working-tree content to the index, and making that comparison can
# run a `clean` filter — which filter runs is named in `.gitattributes`, a
# file this checkout's own tree controls. `git status` can additionally
# invoke an fsmonitor hook named in `.git/config`. A check built to refuse
# unreviewed code must not itself be a way to run some.
#
# `git hash-object --no-filters` hashes a file exactly as it sits on disk,
# with no filter of any kind. Comparing that, path by path, against the blob
# each tracked file resolves to at HEAD answers the same question
# `git diff-index` answers, without the mechanism that makes `git diff-index`
# unsafe to run here: `ls-tree` and `hash-object --no-filters` are both
# content-addressed plumbing that touch neither the index, a hook, nor a
# filter.
#
# TRACKED FILES ONLY. An untracked file sitting in `$DIR` is a different
# hazard from the one this function closes — reviewed code silently not
# being what runs — and is handled separately by check_untracked_shadow
# below, because the two need different comparisons: this one asks whether a
# tracked file's content matches HEAD, not whether a new file exists at all.
#
# COST SCALES WITH TRACKED-FILE COUNT: one `git hash-object` subprocess per
# tracked file, every boot. Measured at 50-100ms across this repository's 72
# tracked files. Worth watching if the tree grows substantially, since this
# runs before every start under `KeepAlive`, not something to re-measure on
# faith.

# Turn a NUL-delimited git listing (`ls-tree -z` / `ls-files -z`) into one
# record per line, so `check_worktree_clean` and `check_untracked_shadow`
# below can read it with a plain `read` loop that runs the same way under
# dash, bash invoked as `sh`, and bash.
#
# `read -r -d ''` — the actual NUL-delimited primitive, and what both
# functions used before this — is a bash-only extension. Dash, which is
# `/bin/sh` on Debian and Ubuntu (including `ubuntu-latest`), rejects `-d`
# outright ("Illegal option -d"). That failure lands in the `while` loop's
# own condition, which is where a `while` is allowed to fail without
# tripping `set -e`, so the loop simply never ran a single iteration:
# `_dirty`/`_found` stayed at their initial 0, and both checks silently
# reported "clean"/"no shadow" no matter what was actually on disk — a
# fail-open, on the exact platform this file exists to run on, with nothing
# printed to say so.
#
# NUL is the one byte git's `-z` output guarantees cannot appear inside a
# record's own fields; a raw newline byte is not excluded from a path, and
# such a path is legal on both platforms. Converting NUL to newline is only
# a safe reframing when nothing already in the data holds a newline, so this
# counts NUL bytes in the source against lines in the converted copy before
# trusting either: an embedded newline makes the two counts disagree, and
# this refuses rather than silently misreading where one record ends and
# the next begins.
reframe_nul_records() {
    _src="$1"
    _dst="$2"
    _what="$3"

    _nul_count=$(tr -cd '\0' < "$_src" | wc -c) \
        || { rm -f "$_src" "$_dst"; die "cannot count records in $_what — refusing"; }
    tr '\0' '\n' < "$_src" > "$_dst" \
        || { rm -f "$_src" "$_dst"; die "cannot reframe $_what for reading — refusing"; }
    rm -f "$_src"
    _line_count=$(grep -c '' "$_dst") || _line_count=0

    [ "$_line_count" -eq "$_nul_count" ] \
        || { rm -f "$_dst"; die "$_what holds a path with a newline byte in it — refusing; this guard cannot tell one record from the next when a path can contain the same byte the records are split on"; }
}

check_worktree_clean() {
    _dir="$1"
    _head="$2"
    _tab=$(printf '\t')

    # A plain temp file, not `< <(...)`. `serve.sh` runs as `/bin/sh`, which on
    # this machine is bash invoked under its name — and bash disables process
    # substitution the moment it believes it is `sh`, so `< <(...)` is a
    # syntax error here, not a portability nicety. A temp file also sidesteps
    # a second problem a plain `| while` would have reintroduced: piping into
    # the loop runs it in a subshell, and `_dirty`, set inside, would vanish
    # the moment the pipe finished.
    _tree="$(mktemp)" || die "cannot create a temp file to list HEAD's tree — refusing"
    git -C "$_dir" ls-tree -r -z "$_head" > "$_tree" \
        || { rm -f "$_tree"; die "cannot list HEAD's tree at $_dir — refusing; set SERVE_ALLOW_BRANCH=1 to bypass"; }

    # Reframed to one record per line (`reframe_nul_records` above, which
    # also takes ownership of removing `$_tree`) rather than read with
    # `read -r -d ''` directly — see that function's comment for why the
    # direct NUL-delimited read silently disabled this whole check under
    # dash.
    _tree_nl="$(mktemp)" || { rm -f "$_tree"; die "cannot create a temp file to reframe HEAD's tree listing — refusing"; }
    reframe_nul_records "$_tree" "$_tree_nl" "$_dir's tracked-file listing at HEAD ($_head)"

    _dirty=0
    while IFS= read -r _rec; do
        _path=${_rec#*"$_tab"}
        _meta=${_rec%%"$_tab"*}
        _mode=${_meta%% *}
        _type=${_meta#* }
        _type=${_type%% *}
        _sha=${_meta##* }

        # A submodule's commit pointer, not a file this check can compare.
        [ "$_type" = blob ] || continue

        # A symlink's tracked content IS the target string, not the bytes at
        # the target. `hash-object --no-filters` follows the link on disk and
        # hashes whatever it points at, so run against a tracked symlink it
        # compares the wrong thing and reports an untouched link as modified
        # forever. Compare the target string on both sides instead, and test
        # with `-L`, never `-e`: `-e` follows the link too and reads false for
        # a symlink whose target does not exist, which is a legitimate state
        # for a symlink to be in and not evidence anything changed.
        if [ "$_mode" = 120000 ]; then
            if [ ! -L "$_dir/$_path" ]; then
                echo "serve: $_dir has $_path (a symlink at HEAD) missing or replaced by a non-symlink since HEAD ($_head)" >&2
                _dirty=1
                continue
            fi
            _actual_target=$(readlink "$_dir/$_path") || _actual_target=""
            _expected_target=$(git -C "$_dir" cat-file -p "$_sha" 2>/dev/null) || _expected_target=""
            if [ "$_actual_target" != "$_expected_target" ]; then
                echo "serve: $_dir has $_path's symlink target modified on disk since HEAD ($_head)" >&2
                _dirty=1
            fi
            continue
        fi

        if [ ! -e "$_dir/$_path" ]; then
            echo "serve: $_dir has $_path removed from disk since HEAD ($_head)" >&2
            _dirty=1
            continue
        fi

        _actual=$(git -C "$_dir" hash-object --no-filters -- "$_path" 2>/dev/null) || _actual=""
        if [ "$_actual" != "$_sha" ]; then
            echo "serve: $_dir has $_path modified on disk since HEAD ($_head)" >&2
            _dirty=1
            continue
        fi

        # Content matches; the executable bit is the one part of a regular
        # tracked file HEAD records that content comparison alone does not
        # cover. Low-impact — Python's import machinery does not consult it —
        # so this is a caveat-closer, not a security boundary the way the
        # content and symlink-target checks above are.
        case "$_mode" in
            100755)
                [ -x "$_dir/$_path" ] \
                    || { echo "serve: $_dir has $_path's executable bit removed since HEAD ($_head)" >&2; _dirty=1; } ;;
            100644)
                [ ! -x "$_dir/$_path" ] \
                    || { echo "serve: $_dir has $_path's executable bit added since HEAD ($_head)" >&2; _dirty=1; } ;;
        esac
    done < "$_tree_nl"
    rm -f "$_tree_nl"

    [ "$_dirty" = 0 ] \
        || die "$_dir's working tree does not match HEAD ($_head) — refusing to serve uncommitted changes; set SERVE_ALLOW_BRANCH=1 to bypass"
}

# An untracked file can shadow a real import, and check_worktree_clean above
# does not see it: an untracked file is not part of HEAD's tree and never
# shows as a diff either, so somebody who can write to $DIR gets past every
# check above by ADDING a file rather than editing one. This is not
# hypothetical — proved by planting an untracked `sqlite_vec.py` here and
# watching `app.py`'s `import sqlite_vec` resolve to it instead of the
# installed package. `serve.sh` `cd`s to `$DIR` and `exec`s
# `python -m uvicorn app:app`, which puts `$DIR` — and only `$DIR` — at
# `sys.path[0]`, ahead of the venv's `site-packages`.
#
# THREE SHAPES ARE CHECKED, deliberately narrow to what can actually shadow
# something. Only `$DIR` itself sits on `sys.path`, not its subdirectories,
# so the hazard is: a top-level `.py` file (`sqlite_vec.py`, shadowing
# `import sqlite_vec` directly); an `__init__.py` exactly one level inside a
# top-level directory (`sqlite_vec/__init__.py`, which makes — or remakes —
# that directory into an importable package, whether or not the directory
# itself is already tracked); and, for the same reason, a `.py` file exactly
# one level inside an EXISTING TRACKED package directory
# (`sqlite_vec/helpers.py`, importable as `sqlite_vec.helpers` the moment
# tracked code does `import sqlite_vec` or `from sqlite_vec import helpers`,
# whether or not `helpers` is ever named by that import). A file two or more
# levels down is not covered: reaching it needs every intermediate directory
# to already be an importable package, which this check does not verify past
# the first level. Refusing on every untracked file instead would fire on
# `.venv`, `__pycache__`, `serve.log` and any other ordinary byproduct of
# working in this directory, which is not a hazard this exists for and would
# get the guard bypassed on sight rather than heeded.
#
# `--exclude-standard` IS NOT USED, though it would filter exactly those
# ordinary byproducts for us. It would also honour this checkout's own
# `.gitignore`, and `.gitignore` is exactly the kind of repository-supplied
# config this file elsewhere refuses to trust: whoever can write an untracked
# shadow file into `$DIR` can just as easily write an untracked `.gitignore`
# pattern that hides it, since neither file is tracked and neither shows up
# in check_worktree_clean above. Listing every untracked file and filtering
# by name and depth here, rather than trusting the checkout to say what is
# safe to ignore, closes that; the narrow scope above is what keeps that
# untrusting choice from being noisy in the ordinary case.
check_untracked_shadow() {
    _dir="$1"

    _list="$(mktemp)" || die "cannot create a temp file to list untracked files — refusing"

    # `-c core.fsmonitor=` on both calls, and the two calls checked
    # separately rather than combined in a `{ }` group. `ls-files --others`
    # invokes a `core.fsmonitor` hook named in `.git/config` exactly the way
    # `git status` does — confirmed by pointing `core.fsmonitor` at a
    # marker-writing script and watching it fire on this call and not on
    # `ls-tree`. `$DIR/.git/config` is itself untracked, so nothing above
    # this function inspects it, and it names neither a `.py` nor an
    # `__init__.py` path, so this function would not have caught it either —
    # whoever can write it could point it at any executable and have it run
    # here, before any refusal, which is the exact hazard
    # `check_worktree_clean` above names `git status` for, reached through a
    # different command. A command-line `-c` overrides repository config,
    # which is what makes the suppression hold against a `.git/config` this
    # checkout's own tree does not control. Checked separately, not as a `{
    # cmd1; cmd2; } > file` group: a shell group's exit status is only its
    # LAST command's, so a failure in the first call would have gone
    # unnoticed and left the guard proceeding on a partial list.
    git -C "$_dir" -c core.fsmonitor= ls-files --others -z -- '*.py' ':(exclude)*/*/*' > "$_list" \
        || { rm -f "$_list"; die "cannot list untracked files at $_dir — refusing; set SERVE_ALLOW_BRANCH=1 to bypass"; }
    git -C "$_dir" -c core.fsmonitor= ls-files --others -z -- '*/__init__.py' ':(exclude)*/*/*' >> "$_list" \
        || { rm -f "$_list"; die "cannot list untracked files at $_dir — refusing; set SERVE_ALLOW_BRANCH=1 to bypass"; }

    # Reframed to one path per line (`reframe_nul_records` above) rather than
    # read with `read -r -d ''` directly — see that function's comment for
    # why the direct NUL-delimited read silently disabled this whole check
    # under dash.
    _list_nl="$(mktemp)" || { rm -f "$_list"; die "cannot create a temp file to reframe the untracked-file listing — refusing"; }
    reframe_nul_records "$_list" "$_list_nl" "$_dir's untracked-file listing"
    rm -f "$_list"

    _found=0
    while IFS= read -r _path; do
        echo "serve: $_dir has an untracked file that can shadow an import: $_path" >&2
        _found=1
    done < "$_list_nl"
    rm -f "$_list_nl"

    [ "$_found" = 0 ] \
        || die "$_dir has untracked files that can shadow a real import — refusing to serve code that was never reviewed; set SERVE_ALLOW_BRANCH=1 to bypass"
}

check_branch "$DIR"

# Everything that could refuse has now run. When LIBRARY_CHECK_PLIST was set,
# the block above already imported `app` in a subprocess and, through
# `store.resolve_db()`, opened `LIBRARY_DB` read-only to fingerprint it —
# the same read every real start already does. Unset, nothing above has
# imported, opened or bound anything.
if [ "$CHECK_ONLY" = 1 ]; then
    echo "serve: --check passed; not starting the service"
    exit 0
fi

# `exec` so launchd's SIGTERM reaches the server process rather than a shell
# that would not forward it, which is what makes a graceful shutdown
# reachable at all.
#
# `serve_dual.py`, not `python -m uvicorn app:app`, as of platform/decisions/
# 0066 stage B: it runs the same plain listener on the same host and port as
# the invocation this replaced, plus a mutual-TLS listener on 127.0.0.1:8443
# that only comes up when this machine has TLS material staged for it —
# serve_dual.py's own module docstring says why a missing listener there
# does not stop this line from serving 8100 exactly as before.
exec "$PY" serve_dual.py
