# Nushell commands for working with upslope. Load with `use upslope.nu` from the repo root (or `use` its full
# path from anywhere), then run e.g. `upslope logs exporter`. Every command works from any directory: paths are
# resolved against this file's location, not the current directory.

const repo = path self .

# The process groups declared in fly.toml, for completion.
def process-groups [] {
    open ($repo | path join fly.toml) | get processes | columns
}

# The migrations in python/migrations/, for completion.
def migrations [] {
    glob ($repo | path join python migrations "m[0-9]*.py") | each { path parse | get stem } | sort
}

# The variables set in the repo's .env.local, as a record for `with-env`. The values are secrets: pass them to
# commands, never print them.
def env-local []: nothing -> record {
    let file = ($repo | path join .env.local)
    if not ($file | path exists) {
        error make {msg: $"($file) not found: copy .env.example and fill it in"}
    }
    open --raw $file
    | lines
    | parse --regex '^\s*(?:export\s+)?(?<key>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?<value>.*?)\s*$'
    | reduce --fold {} {|var, vars|
        let value = if ($var.value =~ '^".*"$' or $var.value =~ "^'.*'$") {
            $var.value | str substring 1..-2
        } else {
            $var.value
        }
        $vars | upsert $var.key $value
    }
}

# Parse the output of `fly logs --json`, which prints each entry as a pretty-printed JSON object spread over
# several lines rather than one object per line. An entry ends at a `}` in the first column: nested objects
# are indented and strings never contain raw newlines, so nothing else can match.
def parse-fly-json []: list<string> -> list<record> {
    generate {|line, buffer|
        let buffer = ($buffer | append $line)
        if $line == "}" {
            {out: ($buffer | str join "\n" | from json), next: []}
        } else {
            {next: $buffer}
        }
    } []
}

# Description of the background jobs `background-lines` spawns, so `logs` can kill them when the TUI exits.
const background_job = "upslope logs"

# Stream the output lines of an external command run in a background job.
#
# An interactive nushell makes a foreground external the terminal's foreground process group, so a `tui run`
# fed by a still-running external is left in the background and its reads of the keyboard fail with EIO
# ("Input/output error"). Externals in a background job never take the terminal; their lines come back
# through the job mailbox, tagged with the job's id so several streams can share it. The job outlives the
# stream: kill it (`job kill`) when done.
def background-lines [command: closure]: nothing -> list<string> {
    let parent = (job id)
    let id = (job spawn --description $background_job {
        let tag = (job id)
        try {
            do $command | lines | each {|line| $line | job send $parent --tag $tag } | ignore
            {done: true} | job send $parent --tag $tag
        } catch {|err|
            {done: true, error: $err.msg} | job send $parent --tag $tag
        }
    })
    generate {|_|
        # poll rather than block: concurrent `job recv`s on one thread queue behind each other, so a quiet
        # stream would otherwise stall every other tab
        let message = try { job recv --tag $id --timeout 100ms } catch { null }
        match $message {
            null => {next: 0}
            {done: true, error: $error} => { error make {msg: $error} }
            {done: true} => {}
            $line => {out: $line, next: 0}
        }
    } 0
}

# Stream the Fly log entries of the given machines as records, following new ones unless no_tail is set.
# With background set, `fly logs` runs in a background job (see `background-lines`), as a TUI needs.
def machine-logs [app: string, machines: table, no_tail: bool, background: bool = false]: nothing -> list<record> {
    let groups = ($machines | reduce --fold {} {|machine, groups|
        $groups | insert $machine.id $machine.config.metadata.fly_process_group
    })
    let ids = ($groups | columns)
    let tail = if $no_tail { [--no-tail] } else { [] }
    # piped, not assigned to a variable: that would collect the stream, which never ends while following
    if $background {
        background-lines { fly logs --app $app --json ...$tail }
    } else {
        fly logs --app $app --json ...$tail | lines
    }
    | parse-fly-json
    | where instance in $ids
    | each {|entry|
        {
            timestamp: ($entry.timestamp | into datetime)
            group: ($groups | get $entry.instance)
            instance: $entry.instance
            message: $entry.message
        }
    }
}

# Show the Fly logs of a process group (web, exporter or archiver) in a TUI, or of every group in tabs.
#
# Follows new entries unless --no-tail is given. In the TUI, `/` searches (Esc clears), scrolling up pauses
# following until you scroll back to the bottom, `[` / `]` switch tabs and `q` quits. --plain returns the
# entries as records instead, for pipelines: `upslope logs exporter -n --plain | where message =~ ERROR`.
export def logs [
    group?: string@process-groups   # Process group, as named under [processes] in fly.toml; omit for every group
    --no-tail (-n)                  # Show the recent logs instead of following
    --plain (-p)                    # Return the entries as records instead of showing them in a TUI
    --app (-a): string = "upslope"  # Fly app
] {
    let machines = (fly machines list --app $app --json | from json)
    let present = ($machines | get config.metadata.fly_process_group | uniq)
    if $group != null and $group not-in $present {
        error make {msg: $"no machines in process group '($group)' of ($app) \(groups: ($present | str join ', '))"}
    }
    let machines = if $group == null { $machines } else {
        $machines | where config.metadata.fly_process_group == $group
    }

    if $plain {
        return (machine-logs $app $machines $no_tail)
    }

    let result = try { {ok: (logs-tui $app $group $machines $no_tail | tui run)} } catch {|err| {error: $err} }
    # the TUI only stops reading the streams; their `fly logs` keep running until killed
    job list | where description == $background_job | each {|job| job kill $job.id } | ignore
    if "error" in $result { error make $result.error }
    $result.ok
}

# The TUI for `logs`, unrun: one log for a group, or a tab per group when group is null. Separate so tests can
# render it with `tui debug`.
def logs-tui [app: string, group: oneof<string, nothing>, machines: table, no_tail: bool] {
    let title = $"upslope · ($group | default 'all groups')"
    # the app's messages already start with their own timestamp and level, and tui log shows strings best
    if $group != null {
        machine-logs $app $machines $no_tail true
        | each { get message }
        | tui label --titlebar $title
        | tui search --bind /
        | tui log --title $group
    } else {
        # a tab per group, in fly.toml's order, each following its own `fly logs`; the search filters every tab
        let present = ($machines | get config.metadata.fly_process_group | uniq)
        let header = (tui label --titlebar $title | tui search --bind /)
        process-groups
        | where $it in $present
        | reduce --fold $header {|name, tui|
            # piped straight into the widget: assigning the stream to a variable would collect it, never returning
            let log = (
                machine-logs $app ($machines | where config.metadata.fly_process_group == $name) $no_tail true
                | each { get message }
                | tui log --title $name
            )
            $tui | tui tab $name [$log]
        }
    }
}

# Run a storage migration from python/migrations/ against the buckets named in .env.local (production).
#
# Arguments after the name go to the migration, e.g. `upslope migrate m002_int16_values strip --dry-run`.
# See python/migrations/README.md before running one.
export def --wrapped migrate [
    name: string@migrations  # Migration module, e.g. m001_webp_frames
    ...args: string          # Arguments and flags for the migration
] {
    # .env.local is loaded here as well as by just, because just's dotenv never overrides a variable already set
    # in the shell: stray AWS_* variables would otherwise point the migration at another bucket
    with-env (env-local) {
        hide-env --ignore-errors AWS_PROFILE  # .env.local's keys are complete; a profile would override them
        ^just --justfile ($repo | path join justfile) py-migrate $name ...$args
    }
}

# The bucket `ls` and `cp` read: the private bucket (BUCKET_NAME, default upslope-storage), or with public the
# public forecasts bucket (PUBLIC_BUCKET_NAME).
def bucket-name [vars: record, public: bool]: nothing -> string {
    let bucket = if $public { $vars.PUBLIC_BUCKET_NAME? } else { $vars.BUCKET_NAME? | default "upslope-storage" }
    if ($bucket | is-empty) {
        error make {msg: "PUBLIC_BUCKET_NAME is not set in .env.local"}
    }
    $bucket
}

# The key prefix of a directory path: a path names a directory, so `forecasts` means forecasts/ rather than every
# key starting with "forecasts".
def dir-prefix [path: string]: nothing -> string {
    let path = ($path | str trim --left --char "/")
    if ($path | is-empty) or ($path | str ends-with "/") { $path } else { $"($path)/" }
}

# Run the aws CLI with the credentials in vars (from `env-local`).
def --wrapped aws-local [vars: record, ...args: string] {
    with-env $vars {
        hide-env --ignore-errors AWS_PROFILE  # .env.local's keys are complete; a profile would override them
        ^aws ...$args
    }
}

# List the keys in one of the S3 buckets named in .env.local, one level at a time like `ls`.
#
# Lists the private bucket (BUCKET_NAME, default upslope-storage) unless --public is given.
export def ls [
    path: string = ""  # Directory (key prefix) to list, e.g. forecasts/hrrr
    --public (-p)      # List the public forecasts bucket (PUBLIC_BUCKET_NAME)
    --recursive (-r)   # List every key under the path instead of one level
]: nothing -> table {
    let vars = (env-local)
    let bucket = (bucket-name $vars $public)
    let prefix = (dir-prefix $path)
    let delimiter = if $recursive { [] } else { [--delimiter /] }

    let listing = (aws-local $vars s3api list-objects-v2 --bucket $bucket --prefix $prefix ...$delimiter --output json)
    # an empty listing prints nothing
    let listing = if ($listing | is-empty) { {} } else { $listing | from json }

    let dirs = ($listing.CommonPrefixes? | default [] | each {|dir|
        {name: ($dir.Prefix | str substring ($prefix | str length)..), type: dir, size: null, modified: null}
    })
    let files = ($listing.Contents? | default [] | each {|file|
        {
            name: ($file.Key | str substring ($prefix | str length)..)
            type: file
            size: ($file.Size | into filesize)
            modified: ($file.LastModified | into datetime)
        }
    })
    $dirs | append $files
}

# Copy a key from one of the S3 buckets named in .env.local to a local file, or with --data return its contents.
#
# Reads the private bucket (BUCKET_NAME, default upslope-storage) unless --public is given. --data parses the
# object by its key's extension, as `open` does a file, e.g. `upslope cp -p -d forecasts/index.json | columns`;
# an unknown extension comes back as raw bytes or text.
export def cp [
    key: string        # Key to copy, e.g. forecasts/index.json, or a directory (key prefix) with --recursive
    dest?: path        # Local file or directory to copy to (default: the key's last part, in the current directory)
    --public (-p)      # Read the public forecasts bucket (PUBLIC_BUCKET_NAME)
    --recursive (-r)   # Copy every key under the directory into the dest directory
    --data (-d)        # Return the object's contents, parsed by its extension, instead of writing a file
]: nothing -> any {
    let vars = (env-local)
    let bucket = (bucket-name $vars $public)

    if $recursive {
        if $data {
            error make {msg: "--data reads a single key; it can't be combined with --recursive"}
        }
        let prefix = (dir-prefix $key)
        let dest = ($dest | default ($prefix | path basename | default --empty $bucket))
        aws-local $vars s3 cp --recursive --no-progress $"s3://($bucket)/($prefix)" $dest
        return
    }

    let key = ($key | str trim --left --char "/")
    if ($key | is-empty) or ($key | str ends-with "/") {
        error make {msg: $"'($key)' is a directory: give a key, or --recursive to copy everything under it"}
    }
    let source = $"s3://($bucket)/($key)"

    if not $data {
        aws-local $vars s3 cp --no-progress $source ($dest | default ($key | path basename))
        return
    }

    # downloaded to a temporary file with the key's extension so `open` picks the same parser it would locally
    let extension = ($key | path parse | get extension)
    let tmp = (mktemp --tmpdir --suffix (if ($extension | is-empty) { "" } else { $".($extension)" }))
    let result = try {
        aws-local $vars s3 cp --only-show-errors $source $tmp
        {ok: (open $tmp)}
    } catch {|err| {error: $err} }
    rm --force $tmp
    if "error" in $result { error make $result.error }
    $result.ok
}
