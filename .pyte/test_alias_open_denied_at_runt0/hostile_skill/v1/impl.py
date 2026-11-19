items = [open]


def run(params):
    return items[0](params['path']).read()
