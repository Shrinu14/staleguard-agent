from server import address

def test_default():
    assert address() == "127.0.0.1:8080"
    assert address(port=9000) == "127.0.0.1:9000"
