
import requests


def get_headers():
    """Gets the headers from the zerotwo website."""
    url = "https://zerotwo.ai/"
    response = requests.get(url)
    print(response.headers)

if __name__ == "__main__":
    get_headers()