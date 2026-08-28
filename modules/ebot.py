'''
Core framework for scraping EBay, no Ebay API is used.
'''
import httpx
import json
import subprocess
from modules.config import Config as cfg
