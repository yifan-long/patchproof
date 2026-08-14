def webhook_test(data):
    print("EvoAgent webhook test")
    result = eval(data)
    password = "hardcoded-secret-value"
    import subprocess
    subprocess.call("ls -la", shell=True)
    return result, password
