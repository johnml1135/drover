"""Answer conversion and prompting for the setup wizard."""

class WizardError(Exception):
    pass


def to_bool(v):
    if isinstance(v, bool):
        return v
    low = str(v).strip().lower()
    if low in ("y", "yes", "true", "1", "on"):
        return True
    if low in ("n", "no", "false", "0", "off"):
        return False
    raise ValueError("answer yes or no")


def to_int(lo=None, hi=None):
    def conv(v):
        try:
            n = int(str(v).strip()) if not isinstance(v, int) or isinstance(v, bool) else v
        except ValueError:
            raise ValueError("a whole number, please") from None
        if (lo is not None and n < lo) or (hi is not None and n > hi):
            raise ValueError(f"between {lo} and {hi}, please")
        return n
    return conv


def to_float(lo=None, hi=None):
    def conv(v):
        try:
            n = float(v)
        except (TypeError, ValueError):
            raise ValueError("a number, please") from None
        if (lo is not None and n < lo) or (hi is not None and n > hi):
            raise ValueError(f"between {lo} and {hi}, please")
        return n
    return conv


def one_of(*options):
    def conv(v):
        v = str(v).strip().lower()
        if v not in options:
            raise ValueError("one of " + ", ".join(options))
        return v
    return conv


def to_str(v):
    return str(v).strip()


def to_list(v):
    return [x.strip() for x in v.split(",") if x.strip()] if isinstance(v, str) else [str(x) for x in v]


class Asker:
    def __init__(self, input_fn, out, answers=None, defaults=False):
        self.input, self.out, self.answers, self.use_defaults = input_fn, out, answers or {}, defaults
        self.existing = {}     # the explicit values of a config.toml already in the state dir: they pre-fill the answers

    def prev(self, dotted, default):
        """The existing config's value for a dotted key, else the detected default."""
        value = self.existing
        for part in dotted.split("."):
            if not isinstance(value, dict) or part not in value:
                return default
            value = value[part]
        return value

    def say(self, text=""):
        print(text, file=self.out)

    def ask(self, key, prompt, default, conv=to_str):
        """The answer for `key`: from the answers file, else the default (--defaults), else the user (empty line = default)."""
        shown = (" [Y/n]" if default else " [y/N]") if isinstance(default, bool) else \
            "" if default in (None, "") else f" [{default}]"
        if key in self.answers:
            try:
                value = conv(self.answers[key])
            except ValueError as e:
                raise WizardError(f"answers file, {key}: {e}") from e
            self.say(f"{prompt}{shown}: {value}  (from answers)")
            return value
        if self.use_defaults:
            value = conv(default) if default not in (None, "") else default
            self.say(f"{prompt}{shown}: {'yes' if value is True else 'no' if value is False else value}  (default)")
            return value
        while True:
            try:
                raw = self.input(f"{prompt}{shown}: ").strip()
            except EOFError:
                raise WizardError("input ended before the questions were answered (use --defaults or --answers)") \
                    from None
            if raw == "" and default not in (None,):
                return conv(default) if default != "" else ""
            try:
                return conv(raw)
            except ValueError as e:
                self.say(f"  {e}")
