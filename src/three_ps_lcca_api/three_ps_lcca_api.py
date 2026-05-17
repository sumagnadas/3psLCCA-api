import json

from typing import Dict, Union
import zipfile

from three_ps_lcca_core.inputs.input import (
    InputMetaData,
    GeneralParameters,
    TrafficAndRoadData,
    VehicleData,
    VehicleMetaData,
    AccidentSeverityDistribution,
    AdditionalInputs,
    MaintenanceAndStageParameters,
    UseStageCost,
    Routine,
    RoutineInspection,
    RoutineMaintenance,
    Major,
    MajorInspection,
    MajorRepair,
    ReplacementCost,
    EndOfLifeStageCosts,
    DemolitionDisposal,
)
from three_ps_lcca_core.inputs.input_global import InputGlobalMetaData
from three_ps_lcca_core.inputs.input import InputMetaData
from three_ps_lcca_core.inputs.wpi import WPIMetaData
from three_ps_lcca_core.core.main import run_full_lcc_analysis
from three_ps_lcca_core.core.utils.input_validator import ironclad_validator
from three_ps_lcca_core.core.utils.list_suggestions import get_IRC_standard_suggestions

from .utils.functions import (
    _decode,
    is_recyclable_valid,
    calc_recovered_value,
    _cached_analysis,
    _cf_value,
    is_carbon_valid,
    calc_carbon,
    calc_vehicle_emission
)

# might not be needed but as placeholder
class ThreePsValidationError(Exception): # just a placeholder and will be implemented if needed
    pass
class ThreePsResults:
    # stagewise results?
    pass

class DataPreparer:
    @staticmethod
    def prepare_life_cycle_construction_cost(data: dict):
        """
        Creates the life cycle construction cost breakdown dict.
        """
        carbon_cost_per_kg = (
            data.get("social_cost_data",{})
            .get("result", {})
            .get("cost_of_carbon_local", {})
        )

        grand_total = 0.0
        super_total = 0.0
        scrap_value = 0.0
        mat_co2     = 0.0
        mat_index   = {}
        trans_co2   = 0.0

        _PAGES = [
            "str_foundation",
            "str_sub_structure",
            "str_super_structure",
            "str_misc",
        ]

        for chunk_id in _PAGES:
            chunk_data = data.get(chunk_id) or {}
            page_total = 0.0

            for comp_name, items in chunk_data.items():
                comp_total = 0.0
                for item in items:
                    # build material index for transport emissions
                    mat_id = item.get("id")
                    if mat_id:
                        mat_index[mat_id] = (item, chunk_id, comp_name)
                    
                    # material cost?
                    state = item.get("state", {})
                    if item.get("state", {}).get("in_trash", False):
                        continue
                    v = item.get("values", {})
                    qty = float(v.get("quantity", 0) or 0)
                    rate = float(v.get("rate", 0) or 0)
                    comp_total += qty * rate

                    # recyclability
                    recycleable_valid = is_recyclable_valid(item)
                    recycleable_included = item.get("state", {}).get(
                        "included_in_recyclability", True
                    )

                    if recycleable_valid and recycleable_included:
                        value = calc_recovered_value(item)
                        scrap_value += value

                    # material emissions
                    carbon_unit = v.get("carbon_unit", "")
                    carbon_denom = (
                        carbon_unit.split("/")[-1] if "/" in carbon_unit else ""
                    )
                    analysis = _cached_analysis(
                        v.get("unit", ""), carbon_denom, _cf_value(v)
                    )
                    valid = is_carbon_valid(item)
                    is_included_flag = item.get("state", {}).get("included_in_carbon_emission", True)
                    is_confirmed = state.get("carbon_conversion_confirmed", False)
                    suspicious = analysis["is_suspicious"] and not is_confirmed

                    if valid and is_included_flag and not suspicious:
                        carbon = calc_carbon(item)
                        mat_co2 += carbon
                page_total += comp_total
            if chunk_id == "str_super_structure":
                super_total = page_total
            grand_total += page_total
        
        trans_co2_data = data.get("transport_data",{})
        vehicles = trans_co2_data.get("vehicles", [])
        # mat_index = _build_material_index(self.controller.engine)

        for entry in vehicles:
            if entry.get("state", {}).get("in_trash", False):
                continue
            emission = calc_vehicle_emission(entry, mat_index)
            trans_co2 += emission
        mach_ems  = data.get("machinery_emissions_data", {})
        mach_mode = mach_ems.get('mode', "")

        diesel_total = 0.0
        elec_total   = 0.0
        if mach_mode == "lumpsum":
            lumpsum = mach_ems.get("lumpsum", {})
            elec_total = lumpsum.get("elec_consumption_per_day", 0) * lumpsum.get("elec_days", 0.0) * lumpsum.get("elec_ef", 0.0)
            diesel_total = lumpsum.get("fuel_consumption_per_day", 0) * lumpsum.get("fuel_days", 0.0) * lumpsum.get("fuel_ef", 0.0)
        elif mach_mode == "detailed":
            detailed = mach_ems["detailed"]
            for row in detailed["rows"]:
                rate = row["row"]
                hrs  = row["hrs"]
                days = row["days"]
                ef   = row["ef"]
                src  = row.get("source","Diesel")

                consumption = rate * hrs * days
                emissions   = consumption * ef

                if src == "Diesel":
                    diesel_total += emissions
                else:
                    elec_total += emissions
        mach_co2 = diesel_total + elec_total
        total_kgCO2e = mat_co2 + trans_co2 + mach_co2
        # super_total = float(data.get("str_super_structure").get("total"))
        # scrap_value = float(data.get("recycling_data").get("total_recovered_value"))

        return {
            "initial_construction_cost": grand_total,
            "initial_carbon_emissions_cost": total_kgCO2e * carbon_cost_per_kg,
            "superstructure_construction_cost": super_total,
            "total_scrap_value": scrap_value,
        }

    @staticmethod
    def prepare_wpi_object(data: dict):
        """
        Creates a WPIMetaData object.
        """
        wpi_data = data.get("traffic_and_road_data").get("wpi")
        wpi_dict = wpi_data.get("data_snapshot").get("ratio")
        year = int(
            wpi_data.get("selected_profile_year")
            or wpi_data.get("selected_profile_name", 0)
        )
        return WPIMetaData.from_dict({"year": year, "WPI": wpi_dict})

    @staticmethod
    def prepare_data_object(data: dict, analysis_period_years: int):
        """
        Creates Core Data Object (InputMetaData or InputGlobalMetaData).
        """
        _financial_data = data.get("financial_data")
        if _financial_data is None:
            raise ValueError(
                "Financial Data is missing from the calculation inputs.\n"
                "Fill in the Financial Data page and try again."
            )

        discount_rate_percent = float(_financial_data.get("discount_rate"))
        inflation_rate_percent = float(_financial_data.get("inflation_rate"))
        interest_rate_percent = float(_financial_data.get("interest_rate"))
        investment_ratio = float(_financial_data.get("investment_ratio"))

        scc_from_fin = _financial_data.get("social_cost_of_carbon")
        if scc_from_fin is not None:
            social_cost_of_carbon_per_mtco2e = float(scc_from_fin) * 1000
        else:
            _result = (
                data.get("carbon_emission_data", {})
                .get("social_cost_data", {})
                .get("result", {})
            )
            social_cost_of_carbon_per_mtco2e = (
                float(_result.get("cost_of_carbon_local", 0.0)) * 1000
            )

        currency_conversion = float(_financial_data.get("currency_conversion", 1.0))

        _bridge_data = data.get("bridge_data")
        service_life_years = int(_bridge_data.get("design_life"))
        construction_period_months = float(
            _bridge_data.get("duration_construction_months")
        )
        working_days_per_month = float(_bridge_data.get("working_days_per_month"))
        days_per_month = float(_bridge_data.get("days_per_month"))

        use_global_road_user_calculations = (
            data.get("traffic_and_road_data").get("mode") == "GLOBAL"
        )

        general_parameters = GeneralParameters(
            service_life_years=service_life_years,
            analysis_period_years=analysis_period_years,
            discount_rate_percent=discount_rate_percent,
            inflation_rate_percent=inflation_rate_percent,
            interest_rate_percent=interest_rate_percent,
            investment_ratio=investment_ratio,
            social_cost_of_carbon_per_mtco2e=social_cost_of_carbon_per_mtco2e,
            currency_conversion=currency_conversion,
            construction_period_months=construction_period_months,
            working_days_per_month=working_days_per_month,
            days_per_month=days_per_month,
            use_global_road_user_calculations=use_global_road_user_calculations,
        )

        _maintenance_data = data.get("maintenance_data")
        _demolition_data = data.get("demolition_data")

        routine_inspection_picc_per_year = float(
            _maintenance_data.get("routine_inspection_cost")
        )
        routine_inspection_interval_in_years = int(
            _maintenance_data.get("routine_inspection_freq")
        )
        routine_maintenance_picc_per_year = float(
            _maintenance_data.get("periodic_maintenance_cost")
        )
        routine_maintenance_picec = float(
            _maintenance_data.get("periodic_maintenance_carbon_cost")
        )
        routine_maintenance_interval_in_years = int(
            _maintenance_data.get("periodic_maintenance_freq")
        )
        major_inspection_picc = float(_maintenance_data.get("major_inspection_cost"))
        major_inspection_interval_in_years = int(
            _maintenance_data.get("major_inspection_freq")
        )
        major_repair_picc = float(_maintenance_data.get("major_repair_cost"))
        major_repair_picec = float(_maintenance_data.get("major_repair_carbon_cost"))
        major_repair_interval_in_years = int(_maintenance_data.get("major_repair_freq"))
        major_repair_duration_months = int(_maintenance_data.get("major_repair_duration"))
        replace_bne_joint_pssc = float(_maintenance_data.get("bearing_exp_joint_cost"))
        replace_bne_joint_interval_in_years = int(
            _maintenance_data.get("bearing_exp_joint_freq")
        )
        replace_bne_joint_duration_in_days = int(
            _maintenance_data.get("bearing_exp_joint_duration")
        )
        eol_picc = float(_demolition_data.get("demolition_cost_pct"))
        eol_picec = float(_demolition_data.get("demolition_carbon_cost_pct"))
        eol_dd_in_months = int(_demolition_data.get("demolition_duration"))

        maintenance_and_stage_parameters = MaintenanceAndStageParameters(
            use_stage_cost=UseStageCost(
                routine=Routine(
                    inspection=RoutineInspection(
                        percentage_of_initial_construction_cost_per_year=routine_inspection_picc_per_year,
                        interval_in_years=routine_inspection_interval_in_years,
                    ),
                    maintenance=RoutineMaintenance(
                        percentage_of_initial_construction_cost_per_year=routine_maintenance_picc_per_year,
                        percentage_of_initial_carbon_emission_cost=routine_maintenance_picec,
                        interval_in_years=routine_maintenance_interval_in_years,
                    ),
                ),
                major=Major(
                    inspection=MajorInspection(
                        percentage_of_initial_construction_cost=major_inspection_picc,
                        interval_for_repair_and_rehabitation_in_years=major_inspection_interval_in_years,
                    ),
                    repair=MajorRepair(
                        percentage_of_initial_construction_cost=major_repair_picc,
                        percentage_of_initial_carbon_emission_cost=major_repair_picec,
                        interval_for_repair_and_rehabitation_in_years=major_repair_interval_in_years,
                        repairs_duration_months=major_repair_duration_months,
                    ),
                ),
                replacement_costs_for_bearing_and_expansion_joint=ReplacementCost(
                    percentage_of_super_structure_cost=replace_bne_joint_pssc,
                    interval_of_replacement_in_years=replace_bne_joint_interval_in_years,
                    duration_of_replacement_in_days=replace_bne_joint_duration_in_days,
                ),
            ),
            end_of_life_stage_costs=EndOfLifeStageCosts(
                demolition_and_disposal=DemolitionDisposal(
                    percentage_of_initial_construction_cost=eol_picc,
                    percentage_of_initial_carbon_emission_cost=eol_picec,
                    duration_for_demolition_and_disposal_in_months=eol_dd_in_months,
                )
            ),
        )

        if not use_global_road_user_calculations:
            _traffic_road_data = data.get("traffic_and_road_data")
            _traffic_vehicle_data = _traffic_road_data.get("vehicle_data")
            _ef = (
                data.get("carbon_emission_data", {})
                .get("diversion_emissions", {})
                .get("emission_factors", {})
            )
            _emission_factors = {k: float(v or 0.0) for k, v in _ef.items()}

            small_cars = VehicleMetaData(
                int(_traffic_vehicle_data.get("small_cars").get("vehicles_per_day")),
                _emission_factors.get("small_cars", 0.0),
                float(_traffic_vehicle_data.get("small_cars").get("accident_percentage")),
            )
            big_cars = VehicleMetaData(
                int(_traffic_vehicle_data.get("big_cars").get("vehicles_per_day")),
                _emission_factors.get("big_cars", 0.0),
                float(_traffic_vehicle_data.get("big_cars").get("accident_percentage")),
            )
            two_wheelers = VehicleMetaData(
                int(_traffic_vehicle_data.get("two_wheelers").get("vehicles_per_day")),
                _emission_factors.get("two_wheelers", 0.0),
                float(_traffic_vehicle_data.get("two_wheelers").get("accident_percentage")),
            )
            o_buses = VehicleMetaData(
                int(_traffic_vehicle_data.get("o_buses").get("vehicles_per_day")),
                _emission_factors.get("o_buses", 0.0),
                float(_traffic_vehicle_data.get("o_buses").get("accident_percentage")),
            )
            d_buses = VehicleMetaData(
                int(_traffic_vehicle_data.get("d_buses").get("vehicles_per_day")),
                _emission_factors.get("d_buses", 0.0),
                float(_traffic_vehicle_data.get("d_buses").get("accident_percentage")),
            )
            lcv = VehicleMetaData(
                int(_traffic_vehicle_data.get("lcv").get("vehicles_per_day")),
                _emission_factors.get("lcv", 0.0),
                float(_traffic_vehicle_data.get("lcv").get("accident_percentage")),
            )
            hcv = VehicleMetaData(
                int(_traffic_vehicle_data.get("hcv").get("vehicles_per_day")),
                _emission_factors.get("hcv", 0.0),
                float(_traffic_vehicle_data.get("hcv").get("accident_percentage")),
                pwr=float(_traffic_vehicle_data.get("hcv").get("pwr")),
            )
            mcv = VehicleMetaData(
                int(_traffic_vehicle_data.get("mcv").get("vehicles_per_day")),
                _emission_factors.get("mcv", 0.0),
                float(_traffic_vehicle_data.get("mcv").get("accident_percentage")),
                pwr=float(_traffic_vehicle_data.get("mcv").get("pwr")),
            )

            minor = float(_traffic_road_data.get("severity_minor"))
            major = float(_traffic_road_data.get("severity_major"))
            fatal = float(_traffic_road_data.get("severity_fatal"))

            alternate_road_carriageway = _traffic_road_data.get("alternate_road_carriageway")
            carriage_width_in_m = float(_traffic_road_data.get("carriage_width_in_m"))
            road_roughness_mm_per_km = float(_traffic_road_data.get("road_roughness_mm_per_km"))
            road_rise_m_per_km = float(_traffic_road_data.get("road_rise_m_per_km"))
            road_fall_m_per_km = float(_traffic_road_data.get("road_fall_m_per_km"))
            additional_reroute_distance_km = float(
                _traffic_road_data.get("additional_reroute_distance_km")
            )
            additional_travel_time_min = float(
                _traffic_road_data.get("additional_travel_time_min")
            )
            crash_rate_accidents_per_million_km = float(
                _traffic_road_data.get("crash_rate_accidents_per_million_km")
            )
            work_zone_multiplier = float(_traffic_road_data.get("work_zone_multiplier"))
            peak_hour_traffic_percent_per_hour = list(
                _traffic_road_data.get("peak_hour_distribution").values()
            )
            hourly_capacity = int(_traffic_road_data.get("hourly_capacity"))
            force_free_flow_off_peak = bool(_traffic_road_data.get("force_free_flow_off_peak"))

            traffic_and_road_data = TrafficAndRoadData(
                vehicle_data=VehicleData(
                    small_cars=small_cars,
                    big_cars=big_cars,
                    two_wheelers=two_wheelers,
                    o_buses=o_buses,
                    d_buses=d_buses,
                    lcv=lcv,
                    hcv=hcv,
                    mcv=mcv,
                ),
                accident_severity_distribution=AccidentSeverityDistribution(
                    minor=minor,
                    major=major,
                    fatal=fatal,
                ),
                additional_inputs=AdditionalInputs(
                    alternate_road_carriageway=alternate_road_carriageway,
                    carriage_width_in_m=carriage_width_in_m,
                    road_roughness_mm_per_km=road_roughness_mm_per_km,
                    road_rise_m_per_km=road_rise_m_per_km,
                    road_fall_m_per_km=road_fall_m_per_km,
                    additional_reroute_distance_km=additional_reroute_distance_km,
                    additional_travel_time_min=additional_travel_time_min,
                    crash_rate_accidents_per_million_km=crash_rate_accidents_per_million_km,
                    work_zone_multiplier=work_zone_multiplier,
                    peak_hour_traffic_percent_per_hour=peak_hour_traffic_percent_per_hour,
                    hourly_capacity=hourly_capacity,
                    force_free_flow_off_peak=force_free_flow_off_peak,
                ),
            )
            return False, InputMetaData(
                general_parameters=general_parameters,
                traffic_and_road_data=traffic_and_road_data,
                maintenance_and_stage_parameters=maintenance_and_stage_parameters,
            )
        else:
            _global_diversion = data.get("carbon_emission_data", {}).get(
                "diversion_emissions", {}
            )
            if _global_diversion.get("mode") == "Calculate by Vehicle":
                total_vehicular_carbon_emission = float(
                    _global_diversion.get("total_calculated_emissions", 0.0)
                )
            else:
                total_vehicular_carbon_emission = float(
                    _global_diversion.get("total_direct_emissions", 0.0)
                )

            total_daily_ruc = float(
                data.get("traffic_and_road_data").get("road_user_cost_per_day")
            )
            daily_road_user_cost_with_vehicular_emissions = DailyRoadUserCost(
                total_daily_ruc=total_daily_ruc,
                total_carbon_emission=TotalCarbonEmission(
                    total_emission_kgCO2e=total_vehicular_carbon_emission
                ),
            )
            return True, InputGlobalMetaData(
                general_parameters=general_parameters,
                daily_road_user_cost_with_vehicular_emissions=daily_road_user_cost_with_vehicular_emissions,
                maintenance_and_stage_parameters=maintenance_and_stage_parameters,
            )


class ThreePsAPI(object):
    _input_data = {}
    _wpi_data = {}
    _lcc_breakdown = {}
    is_global = False
    def __init__(self, input_data: Union[Dict, InputGlobalMetaData, InputMetaData, None] = None, 
                       lcc_breakdown: Union[Dict, None] = None , 
                       wpi_data: Union[Dict, WPIMetaData, None] = None):
        if isinstance(input_data, (InputGlobalMetaData, InputMetaData)):
            self._input_data = input_data.to_dict()
            self.is_global = isinstance(input_data, InputGlobalMetaData)
        else:
            self._input_data = input_data


        self._lcc_breakdown = lcc_breakdown

        if isinstance(wpi_data, WPIMetaData):
            self._wpi_data = wpi_data.to_dict()
        else:
            self._wpi_data = wpi_data

    def set_input_data(self, input_data: Union[Dict, InputGlobalMetaData, InputMetaData, None]):
        if isinstance(input_data, (InputGlobalMetaData, InputMetaData)):
            self._input_data = input_data.to_dict()
        else:
            self._input_data = input_data
    
    def set_lcc_breakdown(self, lcc_breakdown: Union[Dict, None]):
        self._lcc_breakdown = lcc_breakdown

    def set_wpi_data(self, wpi_data: Union[Dict, WPIMetaData, None]):
        if isinstance(wpi_data, WPIMetaData):
            self._wpi_data = wpi_data.to_dict()
        else:
            self._wpi_data = wpi_data
    
    def validate(self):
        suggestions = get_IRC_standard_suggestions()
        
        validation_report = ironclad_validator(
            self._input_data, suggestions, self._wpi_data, eval_wpi=not self.is_global
        )

        return validation_report
    def calculate(self):
        self.results = run_full_lcc_analysis(self._input_data, self._lcc_breakdown, self._wpi_data)
        return self.results
    
    def initial_stage_results(self):
        return self.results['initial_stage']
    
    def use_stage_results(self):
        return self.results['use_stage']
    
    def reconstr_stage_results(self):
        return self.results['reconstruction']
    
    def eol_stage_results(self):
        return self.results['end_of_life']
    
    def load_3pslcca(self, path: Union[str, None]): # will be implemented
        if not path:
            return False
        # try:
            input_data = {}
        with zipfile.ZipFile(path, "r") as zf:
            meta: Dict = {}

            # Fallback to checkpoint_meta.json
            try:
                meta = json.loads(
                    zf.read("manifest.json").decode("utf-8"))
            except:
                pass
            chunks = list(meta.get('chunks', 'N/A').keys())
            chunks_data = {}
            for chunk_name in chunks:
                chunks_data[chunk_name] = _decode(zf.read(f'chunks/{chunk_name}.lcca'))
            analysis_period_years = chunks_data.get('analysis_period',{}).get('analysis_period')
            self.set_input_data(DataPreparer.prepare_data_object(chunks_data, analysis_period_years)[1])
            self.set_wpi_data(DataPreparer.prepare_wpi_object(chunks_data))
            self.set_lcc_breakdown(DataPreparer.prepare_life_cycle_construction_cost(chunks_data))
        return True

if __name__ == "__main__":
    tps_api = ThreePsAPI()
    if tps_api.load_3pslcca('/mnt/Linuxtra/intern_work/3psLCCA-gui/Mumbai-Steel-2L-35m-F.3psLCCA'):
        print(tps_api.calculate())
        