import aiohttp
from typing import Optional, Dict, Any, List

class WorldModelClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8000"):
        self.base_url = base_url

    async def get_tracks(self) -> List[Dict[str, Any]]:
        """
        Hämtar alla aktuella spår (observationsrapporter) från världsmodellen.
        """
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(f"{self.base_url}/api/v1/tracks") as response:
                    if response.status == 200:
                        return await response.json()
                    return []
            except aiohttp.ClientError as e:
                print(f"Kommunikationsfel med B-tjänsten (get_tracks): {e}")
                return []

    async def resolve_target(self, target_text: str, lat: float = 56.1608, lon: float = 15.5872) -> Optional[Dict[str, Any]]:
        """
        Använder B-tjänstens fusionslogik för att översätta mänskligt tal till ett faktiskt track_id.
        """
        payload = {
            "targetText": target_text,
            "operator_lat": lat,
            "operator_lon": lon
        }
        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(f"{self.base_url}/api/v1/resolve", json=payload) as response:
                    if response.status == 200:
                        return await response.json()
                    elif response.status in (404, 503):
                        # 404: Target not found, 503: World model is empty
                        return None
                    else:
                        print(f"Oväntat svar vid resolve_target (HTTP {response.status})")
                        return None
            except aiohttp.ClientError as e:
                print(f"Kommunikationsfel med B-tjänsten (resolve_target): {e}")
                return None

    async def dispatch_task(self, target_id: str, task_type: str, lat: float, lon: float) -> bool:
        """
        Skickar en bekräftad uppgift till B-tjänsten för UDP-multicast-utstötning.
        Returnerar True om B-tjänsten framgångsrikt tog emot och bekräftade kommandot.
        """
        payload = {
            "target_id": target_id,
            "task_type": task_type,
            "latitude": lat,
            "longitude": lon
        }
        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(f"{self.base_url}/api/v1/dispatch", json=payload) as response:
                    return response.status == 200
            except aiohttp.ClientError as e:
                print(f"Kommunikationsfel vid dispatch_task: {e}")
                return False