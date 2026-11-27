"""Detect and authorize exact new direct .NET package references.

A literal package ID is identified case-insensitively. An evaluated Include (an
MSBuild property or item expression) is identified by its exact text, so adding
it still needs the owner's decision.
"""
import re
from xml.etree import ElementTree


PROJECT_SUFFIXES = {'.csproj', '.fsproj', '.vbproj'}
# Imported MSBuild files (Directory.Build.props, Directory.Packages.props, custom .targets) add references too.
IMPORT_SUFFIXES = {'.props', '.targets'}
LEGACY_MANIFEST = 'packages.config'
LEGACY_ROOT, LEGACY_ITEM, LEGACY_ID = 'packages', 'package', 'id'  # <packages><package id=...>
REFERENCE_ITEMS = ('PackageReference', 'GlobalPackageReference')
INCLUDE_LIMIT = 200  # Bounds an approval identity.
MSBUILD = '{http://schemas.microsoft.com/developer/msbuild/2003}'


def matches(path):
    return path.suffix.casefold() in PROJECT_SUFFIXES | IMPORT_SUFFIXES or path.name.casefold() == LEGACY_MANIFEST


def _package(value):
    return (isinstance(value, str) and 0 < len(value) <= INCLUDE_LIMIT and value.strip() == value
            and ';' not in value and value.isprintable())


def target(value):
    if set(value) != {'path', 'package'} or not _package(value['package']):
        raise ValueError('A .NET package decision requires a project path and the package as referenced')
    return (value['package'].casefold(),)


def _names(include):
    names = [part.strip() for part in (include or '').split(';') if part.strip()]
    if not all(_package(name) for name in names):
        raise ValueError('Unsupported package reference')
    return {(name.casefold(),) for name in names}


def read_targets(content):
    # MSBuild manifests need neither declarations of entities nor DTDs.
    if '\0' in content or '<!DOCTYPE' in content:
        raise ValueError('Expected a UTF-8 MSBuild project without a DOCTYPE')
    root = ElementTree.fromstring(content)
    if root.tag == LEGACY_ROOT:
        packages = set()
        for item in root.findall(LEGACY_ITEM):
            packages |= _names(item.get(LEGACY_ID))
        return packages
    if root.tag not in {'Project', MSBUILD + 'Project'}:
        raise ValueError('Expected an MSBuild Project document')
    namespace = MSBUILD if root.tag.startswith(MSBUILD) else ''
    packages = set()
    for group in root.iter(namespace + 'ItemGroup'):
        for kind in REFERENCE_ITEMS:
            for item in group.findall(namespace + kind):
                packages |= _names(item.get('Include'))  # Update and Remove change existing items.
    return packages
