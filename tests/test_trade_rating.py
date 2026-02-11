import unittest
from src.trade_rating import compute_rate, triangular, clamp

class TestTradeRating(unittest.TestCase):
    
    def test_clamp(self):
        self.assertEqual(clamp(5, 0, 10), 5)
        self.assertEqual(clamp(-5, 0, 10), 0)
        self.assertEqual(clamp(15, 0, 10), 10)

    def test_triangular(self):
        # Peak
        self.assertAlmostEqual(triangular(4, 2, 4, 8), 1.0)
        # Left edge
        self.assertAlmostEqual(triangular(2, 2, 4, 8), 0.0)
        # Right edge
        self.assertAlmostEqual(triangular(8, 2, 4, 8), 0.0)
        # Middle left (3 is mid of 2-4) -> 0.5
        self.assertAlmostEqual(triangular(3, 2, 4, 8), 0.5)

    def test_compute_rate_perfect_scenario(self):
        # Scenario: 
        # Explosion: 40% -> 4 pts
        # Volume: Rank 1.0 -> 2 pts
        # Pullback: 4% -> 2 pts
        # Support: 0 dist -> 2 pts
        # Funding: 0 -> 0 pts
        # Bonus: regime 1, micro 1 -> +1 (clamped)
        
        res = compute_rate(
            change24h_pct=40.0,
            quote_volume=1000000,
            volume_rank=1.0,
            last_price=100.0,
            recent_high=104.16, # approx 4% pullback: (104.16 - 100)/104.16 =~ 0.0399
            nearest_support=100.0, # 0% dist
            funding_rate=0.0001,
            vol_z=3.0, # bonus
            wick_ratio=0.5 # bonus
        )
        # 4 + 2 + ~2 + 2 + 0 + 1 = 11 -> clamped to 10
        self.assertEqual(res.rate, 10.0)
        self.assertEqual(res.components.explosion, 4.0)

    def test_compute_rate_basic(self):
        # Explosion: 24% (mid of 8-40) -> ~2 pts
        # Volume: 0.5 -> 1 pt
        # No heavy features
        res = compute_rate(
            change24h_pct=24.0,
            quote_volume=100000,
            volume_rank=0.5,
            last_price=10.0
        )
        # 2 + 1 = 3.0
        self.assertEqual(res.rate, 3.0)

    def test_funding_penalty(self):
        # Base 5 pts
        # Funding penalty -2
        res = compute_rate(
            change24h_pct=40.0, # 4
            quote_volume=100000,
            volume_rank=0.5, # 1
            last_price=100,
            funding_rate=0.006 # > 0.5% -> -2
        )
        # 4 + 1 - 2 = 3.0
        self.assertEqual(res.rate, 3.0)

if __name__ == '__main__':
    unittest.main()
