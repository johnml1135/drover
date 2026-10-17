"""python -m drover <init|config> ...: the setup wizard and the config tool. Other modules run as python -m drover.<name>."""
import sys

USAGE = """usage: python -m drover init [--defaults] [--answers FILE.toml] [--state-dir DIR]
       python -m drover config [sync [--json]] [--show] [--get KEY] [--check FILE]"""


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] == ["init"]:
        from . import wizard
        return wizard.main(argv[1:])
    if argv[:1] == ["config"]:
        from . import config
        return config.main(argv[1:])
    print(USAGE, file=sys.stderr)
    return 0 if argv[:1] in (["-h"], ["--help"]) else 2


if __name__ == "__main__":
    sys.exit(main())
