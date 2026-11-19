def run(params):
    buf = bytearray(8 * 1024 * 1024 * 1024)
    return {'len': len(buf)}
