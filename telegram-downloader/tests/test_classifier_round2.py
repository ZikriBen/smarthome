import unittest
from app.classifier import classify_media

class TestClassifierRound2(unittest.TestCase):
    def test_remaining_prefixes(self):
        cases = [
            ('480p נתי מדיה בתולות ע3 פ3', 'בתולות'),
            ('NF 480p נתי מדיה גולסטאר ע4 פ19', 'גולסטאר'),
            ('480p יוסי סרטים גרים ע6 פ6', 'גרים'),
            ('כל הסדרות המפץ הגדול ע11פ1_480P', 'המפץ הגדול'),
            ('מדיה VOD התנהגות טובה ע2פ1_480P', 'התנהגות טובה'),
            ('קינג סרט ענבים חמוצים 2016 480p WEBRip', 'ענבים חמוצים'),
            ('480p נ.מדיה לא נפסיק לשיר ולרקוד 2020', 'לא נפסיק לשיר ולרקוד'),
        ]
        for text, title in cases:
            with self.subTest(text=text):
                self.assertEqual(classify_media(filename=text, caption=None).title, title)

    def test_conflicting_sources(self):
        for filename, caption in [
            ('Show S01E02.mkv', 'Show S02E02'),
            ('Show S01E02.mkv', 'Show S01E03'),
            ('Show Ep 2.mkv', 'Show S03E04'),
        ]:
            with self.subTest(filename=filename, caption=caption):
                self.assertEqual(classify_media(filename=filename, caption=caption).media_type.value, 'UNKNOWN')

    def test_same_source_title(self):
        r = classify_media(filename='Other Ep 7.mkv', caption='Show S03E07')
        self.assertEqual((r.title,r.episode.season), ('Show',3))

    def test_reject_unrepresentable_sequences(self):
        for sequence in ('S01E03+05','S01E03+04+05','S01E03E05','S01E05-E03','S01E03E04E05'):
            with self.subTest(sequence=sequence):
                self.assertEqual(classify_media(filename=f'Show 2020 {sequence}.mkv',caption=None).media_type.value,'UNKNOWN')

    def test_supported_sequences(self):
        for sequence, end in [('S01E03+04',4),('S01E03E04',4),('S01E03-E05',5)]:
            with self.subTest(sequence=sequence):
                r=classify_media(filename=f'Show {sequence}.mkv',caption=None)
                self.assertEqual((r.media_type.value,r.episode.episode_start,r.episode.episode_end),('TV',3,end))

    def test_rejected_tv_does_not_become_movie(self):
        r=classify_media(filename='Show 2020 Ep 2.mkv',caption=None,auto_tv_threshold=.9)
        self.assertEqual(r.media_type.value,'UNKNOWN')

    def test_legitimate_title_words_remain(self):
        for title in ('סרטים של החיים','מדיה אחרת','נתי מדיהלי','כל הסדרותיות'):
            with self.subTest(title=title):
                r=classify_media(filename=f'{title} S01E01.mkv',caption=None)
                self.assertEqual(r.title,title)

if __name__=='__main__':
    unittest.main()
