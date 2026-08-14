/**
 * The agreed winding-time formulas, ported verbatim from `js/calculator.js`
 * of StanisRyz/calculate @ d32eae0. Only the two exported names moved onto
 * the `windingCalculator` namespace; every coefficient, cap, rounding step,
 * calibration branch and hoop-geometry rule is unchanged.
 *
 * This is the source of truth for the mathematics. Changing anything here
 * requires a new `CALCULATION_VERSION` in `calculator/models.py`.
 */
(function () {
  var api = window.windingCalculator = window.windingCalculator || {};

  function capped(value, maximum) { return Math.min(value, maximum); }
  function diameterTierFactor(diameterMm, tiers) {
    for (var index = 0; index < tiers.length; index++) if (tiers[index].maximumMm == null || diameterMm <= tiers[index].maximumMm) return tiers[index].factor;
    return tiers[tiers.length - 1].factor;
  }
  api.normalizeComplexityCoefficient = function (value) { return Math.round(Number(value) * 4) / 4; };

  api.calculateWinding = function (input) {
    var rules = api.rules;
    var additional = rules.additionalOperations;
    var heightMm = input.windingHeightMm;
    var massKg = Math.PI * (input.D * input.D - input.d * input.d) / 4 * input.b * rules.densityKgPerCubicMm;
    var standard = rules.windingSpeedBase.secondsPerMm * rules.windingSpeedBase.thicknessMm / input.tapeThicknessMm;
    var windingTimeSeconds = heightMm * standard;
    var baseAdditionalTimeFromMassSeconds = input.d < additional.baseTime.innerDiameterThresholdMm
      ? additional.baseTime.belowThresholdSeconds
      : Math.min(additional.baseTime.massSquaredCoefficient * massKg * massKg + additional.baseTime.offsetSeconds, additional.baseTime.maximumSeconds);
    var calibrationTimeSeconds = input.calibration === 'Да' ? additional.calibrationTime.diameterCoefficientSecondsPerMm * input.calibrationDiameterMm + additional.calibrationTime.offsetSeconds : 0;
    var baseAdditionalTimeSeconds = baseAdditionalTimeFromMassSeconds + calibrationTimeSeconds;
    var widthMultiplier = input.b < additional.width.lowerBoundaryMm
      ? additional.width.belowCoefficient * Math.pow(input.b - additional.width.lowerBoundaryMm, 2) + 1
      : input.b <= additional.width.upperBoundaryMm ? 1 : additional.width.aboveCoefficient * Math.pow(input.b - additional.width.upperBoundaryMm, 2) + 1;
    var thicknessMultiplier = additional.thickness.coefficient * Math.pow(input.tapeThicknessMm - additional.thickness.referenceMm, 2) + 1;
    var massMultiplier = capped(additional.mass.coefficient * Math.pow(massKg - additional.mass.referenceKg, 2) + 1, additional.mass.maximum);
    var heightMultiplier = capped(additional.height.coefficient * Math.pow(heightMm - additional.height.referenceMm, 2) + 1, additional.height.maximum);
    var innerDiameterMultiplier = input.d < additional.innerDiameter.boundaryMm
      ? additional.innerDiameter.lowCoefficient * Math.pow(input.d - additional.innerDiameter.lowReferenceMm, 2) + additional.innerDiameter.lowOffset
      : capped(additional.innerDiameter.highCoefficient * Math.pow(input.d - additional.innerDiameter.boundaryMm, 2) + 1, additional.innerDiameter.highMaximum);
    var hoopRules = rules.hoopGeometry;
    var hoopRatio = input.d / heightMm;
    var hoopDiameterFactor = diameterTierFactor(input.d, hoopRules.diameterTiers);
    var ratioPenalty = hoopRules.coefficient * Math.pow(Math.max(0, hoopRatio - hoopRules.referenceRatio), 2);
    var rawHoopMultiplier = 1 + ratioPenalty * hoopDiameterFactor;
    var hoopMultiplier = Math.min(hoopRules.maximum, Math.max(hoopDiameterFactor, rawHoopMultiplier));
    var rawAdditionalTimeSeconds = baseAdditionalTimeSeconds * widthMultiplier * thicknessMultiplier * massMultiplier * heightMultiplier * innerDiameterMultiplier * hoopMultiplier;
    var additionalOperationsTimeSeconds = rawAdditionalTimeSeconds;
    var rawComplexityCoefficient = (windingTimeSeconds + additionalOperationsTimeSeconds) / windingTimeSeconds;
    return { heightMm: heightMm, massKg: massKg, standard: standard, windingTimeSeconds: windingTimeSeconds,
      baseAdditionalTimeFromMassSeconds: baseAdditionalTimeFromMassSeconds, calibrationTimeSeconds: calibrationTimeSeconds, baseAdditionalTimeSeconds: baseAdditionalTimeSeconds, widthMultiplier: widthMultiplier, thicknessMultiplier: thicknessMultiplier,
      massMultiplier: massMultiplier, heightMultiplier: heightMultiplier, innerDiameterMultiplier: innerDiameterMultiplier,
      hoopRatio: hoopRatio, hoopDiameterFactor: hoopDiameterFactor, hoopMultiplier: hoopMultiplier,
      additionalOperationsTimeSeconds: additionalOperationsTimeSeconds,
      totalTimeSeconds: windingTimeSeconds + additionalOperationsTimeSeconds,
      rawComplexityCoefficient: rawComplexityCoefficient,
      complexityCoefficient: api.normalizeComplexityCoefficient(rawComplexityCoefficient) };
  };
})();
