"""Fetch a selected vendor skill at a resolved commit; never run its scripts."""
import argparse
import json
import os
from pathlib import Path
import urllib.request
import datetime
import shutil
import hashlib
import io
import stat
import zipfile
from pathlib import PurePosixPath

try:
    from .install import (project_target, project_skill_config, file_target, directory_target, git_top,
                          is_framework_source, private_exclude, check_ignored)
except ImportError:  # Direct script invocation.
    from install import (project_target, project_skill_config, file_target, directory_target, git_top,
                         is_framework_source, private_exclude, check_ignored)

REGISTRY = {
 'mastra': ('mastra-ai/skills', ['skills/mastra']),
 'angular': ('angular/skills', ['angular-developer']),
 'agent-framework': ('microsoft/agent-framework', ['skills/agent-framework-builder-dotnet']),
 'azure-dotnet': ('microsoft/skills', [f'.github/plugins/azure-sdk-dotnet/skills/{name}' for name in ('azure-ai-openai-dotnet', 'azure-search-documents-dotnet', 'azure-servicebus-dotnet')]),
 'azure-functions': ('MicrosoftDocs/Agent-Skills', ['skills/azure-functions']),
}


def package_files(content, paths):
    """Read pinned ZIP bytes in memory; never extract links or execute scripts."""
    files = {}
    total = 0
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as error:
        raise ValueError('Pinned vendor package is not a valid ZIP archive') from error
    with archive:
        roots = set()
        for entry in archive.infolist():
            name = entry.filename
            path = PurePosixPath(name)
            if (path.is_absolute() or '\\' in name or '\x00' in name
                    or path.as_posix() != name.rstrip('/')
                    or any(part in ('..', '.') or ':' in part for part in path.parts)):
                raise ValueError('Vendor archive contains an unsafe path')
            if not path.parts:
                raise ValueError('Vendor archive contains an empty path')
            roots.add(path.parts[0])
            mode = entry.external_attr >> 16
            if stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR):
                raise ValueError('Vendor archive contains a link or special file')
            total += entry.file_size
            if total > 100 * 1024 * 1024:
                raise ValueError('Vendor package exceeds the unpacked size limit')
            relative = PurePosixPath(*path.parts[1:])
            for source in paths:
                prefix = PurePosixPath(source)
                if relative.is_relative_to(prefix) and not entry.is_dir():
                    tail = relative.relative_to(prefix)
                    target = str(PurePosixPath(prefix.name) / tail)
                    if any(os.path.normcase(target) == os.path.normcase(prior) for prior in files):
                        raise ValueError('Vendor archive has duplicate destination files')
                    files[target] = archive.read(entry)
        if len(roots) != 1:
            raise ValueError('Vendor archive must have one package root')
    for source in paths:
        entrypoint = PurePosixPath(source).name + '/SKILL.md'
        if entrypoint not in files:
            raise ValueError('Pinned vendor package is missing ' + entrypoint)
        text = files[entrypoint].decode('utf-8')
        if not text.startswith('---') or '\nname:' not in text:
            raise ValueError('Pinned package contains an invalid skill entrypoint')
    owned = {os.path.normcase(str(Path(name))) for name in files}
    for name in files:
        if any(os.path.normcase(str(parent)) in owned for parent in Path(name).parents):
            raise ValueError('Vendor archive file is also a destination directory')
    return files

def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--stack', choices=REGISTRY, required=True)
    destination = p.add_mutually_exclusive_group()
    destination.add_argument('--dest', type=Path, help='Explicit skill destination')
    destination.add_argument('--project', type=Path, help='Activate vendor skills in one existing project')
    destination.add_argument('--global', dest='global_install', action='store_true', help='Explicit global vendor installation')
    p.add_argument('--ref', help='Reviewed immutable commit; required for installation')
    p.add_argument('--inspect', action='store_true', help='Print resolved source and SKILL.md without installation')
    p.add_argument('--method', choices=['auto', 'download'], default='auto', help='Compatibility option; pinned packages use the stdlib ZIP copier')
    a = p.parse_args(argv)
    if not a.inspect and not (a.dest or a.project or a.global_install):
        p.error('Select --dest, --project or --global; inspection alone needs no destination')
    if not a.inspect and not a.ref:
        p.error('Installation requires --ref with a reviewed full commit SHA')
    repo, paths = REGISTRY[a.stack]
    project = None
    config_write = None
    if a.project:
        project = a.project.resolve(strict=True)
        if not project.is_dir():
            p.error('--project requires an existing directory')
        a.dest = directory_target(project_target(project, project / '.agents/skills'))
        entries = [file_target(project_target(project, a.dest / Path(path).name / 'SKILL.md')) for path in paths]
        config_write = project_skill_config(project, entries)
        directory_target(project_target(project, project / '.codex/autonomous-se-backups'))
        file_target(project_target(project, a.dest / ('ase-vendor-' + a.stack + '.json')))
    elif a.global_install:
        a.dest = Path.home() / '.agents/skills'
    if a.dest is not None:
        a.dest = a.dest.expanduser().resolve()
        directory_target(a.dest)
        for path in paths:
            file_target(a.dest / Path(path).name / 'SKILL.md')
        file_target(a.dest / ('ase-vendor-' + a.stack + '.json'))
        outer = git_top(a.dest) if project is None else None
        if outer is not None and not is_framework_source(outer):
            # Only --project knows how to keep a pack out of that repository's history.
            p.error('The destination is inside a Git repository; use --project so the pack stays private')
    def fetch(url):
        req = urllib.request.Request(url, headers={'User-Agent': 'autonomous-se/0.1'})
        with urllib.request.urlopen(req, timeout=60) as response:
            data = response.read(100 * 1024 * 1024 + 1)
            if len(data) > 100 * 1024 * 1024:
                raise ValueError('Vendor download exceeds the package size limit')
            return data
    ref = a.ref or json.loads(fetch(f'https://api.github.com/repos/{repo}/commits/HEAD'))['sha']
    if len(ref) != 40 or any(c not in '0123456789abcdef' for c in ref.lower()):
        raise SystemExit('--ref must be an immutable full commit SHA')
    if a.inspect:
        for path in paths:
            content = fetch(f'https://raw.githubusercontent.com/{repo}/{ref}/{path}/SKILL.md').decode('utf-8')
            print(json.dumps({'repo': repo, 'ref': ref, 'path': path}))
            print(content)
        return
    content = fetch(f'https://codeload.github.com/{repo}/zip/{ref}')
    files = package_files(content, paths)
    lock = {'repo': repo, 'commit': ref, 'paths': paths,
            'archive_sha256': hashlib.sha256(content).hexdigest(),
            'files': {name: hashlib.sha256(data).hexdigest() for name, data in sorted(files.items())}}
    writes = [(a.dest / name, data) for name, data in sorted(files.items())]
    writes.append((a.dest / ('ase-vendor-' + a.stack + '.json'), json.dumps(lock, indent=2).encode('utf-8')))
    if config_write:
        writes.append(config_write)
    scope = project or a.dest
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    backup_root = (project / '.codex/autonomous-se-backups' if project else a.dest / '.ase-vendor-backups') / stamp
    changes = []
    exclude = None
    top = git_top(project) if project else None
    if top is not None and not is_framework_source(top):
        # Vendor packs, the skill selection and backups stay out of the project's Git history.
        exclude = private_exclude(project, [target for target, _ in writes] + [project / '.codex/autonomous-se-backups/x'],
                                  folders=['.agents/skills'], wholes=['.codex/autonomous-se-backups'])
        file_target(project_target(exclude['scope'], exclude['path']))
        if not exclude['path'].exists() or exclude['path'].read_bytes() != exclude['data']:
            changes.append((exclude['path'], exclude['data'], None))
    # The entire downloaded tree and every backup path are checked before the
    # first mutation; discovering a late conflict cannot leave half a package.
    for index, (target, data) in enumerate(writes):
        file_target(project_target(scope, target))
        if not target.exists() or target.read_bytes() != data:
            backup = backup_root / str(index) / target.name if target.exists() else None
            if backup:
                file_target(project_target(scope, backup))
            changes.append((target, data, backup))
    checked = exclude is None
    for target, data, backup in changes:
        if not checked and target != exclude['path']:
            check_ignored(exclude['top'], exclude['paths'])
            checked = True
        file_target(project_target(exclude['scope'] if exclude and target == exclude['path'] else scope, target))
        if backup:
            file_target(project_target(scope, backup))
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, backup)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    print(json.dumps({'repo': repo, 'commit': ref, 'destination': str(a.dest),
                      'changed': [str(target) for target, _, _ in changes]}, indent=2))


if __name__ == '__main__':
    main()
