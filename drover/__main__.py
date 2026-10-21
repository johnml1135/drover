"""python -m drover <init|config> ...: the setup wizard and the config tool. Other modules run as python -m drover.<name>."""
import sys

USAGE = """usage: python -m drover init [--defaults] [--answers FILE.toml] [--state-dir DIR]
       python -m drover config [sync [--json]] [--show] [--get KEY] [--check FILE]
       python -m drover config explain KEY [--repo NAME] [--revision REV]
       python -m drover config migrate-registration NAME [--revision REV] [--apply]
       python -m drover worktree add PATH REF --repo REPO --owner OWNER --lease-until ISO8601
       python -m drover disk-tracker read|tick|install [--dry-run|--apply]
       python -m drover disk-cleanup [--mode daily|on-demand|triggered] [--dry-run|--apply] [--json]"""


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] == ["init"]:
        from . import wizard
        return wizard.main(argv[1:])
    if argv[:1] == ["config"]:
        from . import config
        return config.main(argv[1:])
    if argv[:1] == ["worktree"]:
        from . import worktree
        return worktree.main(argv[1:])
    if argv[:1] == ["disk-tracker"]:
        from . import disk
        return disk.main(argv[1:])
    if argv[:1] == ["disk-cleanup"]:
        from . import disk_cleanup
        return disk_cleanup.main(argv[1:])
    print(USAGE, file=sys.stderr)
    return 0 if argv[:1] in (["-h"], ["--help"]) else 2


if __name__ == "__main__":
    sys.exit(main())
