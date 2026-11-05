from slug import slugify

def test_slugify():
    assert slugify("Hello  World!") == "hello-world"
    assert slugify("Python 3 Tips") == "python-3-tips"
    assert slugify("hello world") == "hello-world"
