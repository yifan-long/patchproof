import os

def webhook_test(data):
    result = eval(data)
    password = os.environ['PASSWORD']
    import subprocess
    subprocess.call('ls -la', shell=False)
    return (result, password)
