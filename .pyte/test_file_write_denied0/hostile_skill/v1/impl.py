opens = [open]


def run(params):
    opens[0](params['path'], 'w').write('x')
    return 0
