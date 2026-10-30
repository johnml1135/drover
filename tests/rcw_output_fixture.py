"""Output identity operations for the in-memory checkout fixture."""
def resolve(self, path, ref):
    return self.fixture(path).head(ref)

def ancestor(self, path, base, tip):
    return self.fixture(path).ancestor(base, tip)

def changes(self, path):
    return [p for p, *_ in self.fixture(path).status(path)]

def trailers(self, path):
    import re
    f = self.fixture(path)
    message = f.commits[f.head(cwd=path)]['message']
    return [next(iter(re.findall('^' + name + r': (.*)$', message, re.M)), '')
            for name in ('Drover-Task', 'Drover-Run')]

def commit(self, path, message):
    return self.fixture(path).save(path, message)
