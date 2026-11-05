from fizz import fizzbuzz

def test_all():
    assert [fizzbuzz(i) for i in (1, 3, 5, 15, 30, 7)] == ["1", "Fizz", "Buzz", "FizzBuzz", "FizzBuzz", "7"]
