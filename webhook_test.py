def webhook_test(data):
    print("EvoAgent webhook test")
    result = eval(data)
    password = "hardcoded-secret-value"
    return result, password
