# Test candidate sorting logic
mock_candidates = [
    {'offer_id': '1', 'price_value': 700.0, 'matched_skins': []},
    {'offer_id': '2', 'price_value': 1500.0, 'matched_skins': ['eon']},
    {'offer_id': '3', 'price_value': 4550.0, 'matched_skins': ['black_knight']},
    {'offer_id': '4', 'price_value': 1200.0, 'matched_skins': []},
    {'offer_id': '5', 'price_value': 2800.0, 'matched_skins': ['dark_vertex']},
]

def candidate_sort_key(c):
    # 0 if c has rare skins (comes first), 1 otherwise
    has_rare_skin = 0 if c.get('matched_skins') else 1
    return (has_rare_skin, c['price_value'])

mock_candidates.sort(key=candidate_sort_key)

print("Sorted candidates:")
for c in mock_candidates:
    print(f"ID: {c['offer_id']}, Price: {c['price_value']}, Skins: {c['matched_skins']}")
