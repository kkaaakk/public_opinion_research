# Tesla Battery Technical Facts — Internal Reference

## Battery Chemistry by Model & Region

| Model | Market | Supplier | Chemistry | Cell Format | Pack Voltage |
|-------|--------|----------|-----------|-------------|-------------|
| Model 3/Y RWD | China | CATL | LFP | Prismatic | ~350V |
| Model 3/Y RWD | Global | CATL | LFP | Prismatic | ~350V |
| Model 3/Y LR/Perf | China | LG Energy Solution | NCM 811 | 2170 Cylindrical | ~360V |
| Model 3/Y LR/Perf | US | Panasonic | NCA | 2170 / 4680 | ~360V |
| Model S/X | Global | Panasonic | NCA | 18650 | ~400V |
| Cybertruck | US | Panasonic | NCA | 4680 | ~800V |

## Battery Management System (BMS) Facts

1. **SOC Buffer**: Tesla maintains a top buffer (~4.5% on NCA, ~2.5% on LFP) that is not displayed to the driver. This buffer protects against overcharge and enables regenerative braking at high SOC.
2. **Thermal Management**: Liquid-cooled thermal system maintains battery temperature between 15°C-45°C during normal operation. Below -10°C, the BMS limits discharge power until the pack warms up.
3. **OTA Calibration**: BMS calibration parameters can be adjusted via OTA. This is standard practice to improve SOC estimation accuracy, not to "lock" capacity.
4. **Supercharging Impact**: After ~500 DC fast-charge sessions on NCA packs, the BMS may reduce peak charging power by ~5-10% to manage cell aging. This is documented in owner's manuals since 2019.
5. **LFP Charging Guidance**: LFP packs should be charged to 100% at least once per week for BMS calibration. The vehicle displays this recommendation.

## GB38031-2025 Compliance Status (Internal)

- **Effective Date**: 2026-07-01
- **Key Requirement**: Battery pack must not ignite or explode within 5 minutes of thermal runaway warning (previously "within 5 minutes of fire").
- **Tesla Status**: All China-market vehicles produced after 2026-01-01 have been validated against GB38031-2025 test protocols. Engineering sign-off completed 2025-12-15.
- **Legacy vehicles**: 2024 and earlier models meet GB38031-2020. No regulatory requirement to retrofit. Communication plan drafted, pending PR approval.

## Warranty Key Points

- Battery & Drive Unit warranty: 8 years, mileage varies by model (100k/120k/150k miles)
- Capacity retention: minimum 70% during warranty period
- Replacement policy: Tesla may use remanufactured packs that meet or exceed 70% capacity
- Self-paid battery replacement comes with 4-year/50k-mile warranty on the new pack
- Extended battery warranty program launching 2026 (details: flexible monthly subscription, est. $100-200/month)

## Common Misconceptions

1. **"OTA reduces range to hide defects"**: BMS calibration updates improve accuracy; they do not reduce physical capacity. Apparent range changes after updates reflect more accurate estimation, not capacity loss.
2. **"LFP is inferior"**: LFP has lower energy density but superior cycle life (>3000 cycles vs ~1500 for NCA) and better thermal stability.
3. **"70% threshold never triggers"**: Internal data shows <0.3% of vehicles trigger the 70% warranty replacement threshold during warranty period.
