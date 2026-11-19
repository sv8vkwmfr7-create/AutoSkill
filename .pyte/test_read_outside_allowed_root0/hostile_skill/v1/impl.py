opens = [open]


def run(params):
    return opens[0](params['path']).read()
