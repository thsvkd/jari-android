"""The stations worth putting on buttons.

Korail is the only railway the bot books with, so there is one list. It was
not always so: SR ran the 수서 line as a separate company, with its own API,
its own account and its own set of stations, and every search had to say which
of the two it was against. That ended on 2026-09-01, when the two merged -
수서발 trains are KTX now, sold by Korail, and both the SRT app and the API
behind it were retired.

수서, 동탄 and 평택지제 are on this list because of that merger. They used to
be SR's alone; they are Korail stations now, and a booking from any of them
goes through the same client as one from 서울.
"""

#: The ones worth a button, in the order someone scans them. Every name here
#: has to be one Korail knows - a test checks them against the station table,
#: because a button that fails validation would be worse than no button.
MAJOR_STATIONS = (
    "서울",
    "용산",
    "청량리",
    "수서",
    "동탄",
    "평택지제",
    "광명",
    "천안아산",
    "오송",
    "대전",
    "동대구",
    "부산",
    "울산(통도사)",
    "포항",
    "익산",
    "전주",
    "광주송정",
    "목포",
    "여수EXPO",
    "강릉",
)
