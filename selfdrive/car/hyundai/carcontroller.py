from cereal import car, log
from common.realtime import DT_CTRL
from common.numpy_fast import clip, interp
from common.conversions import Conversions as CV
from selfdrive.car import apply_std_steer_torque_limits
from selfdrive.car.hyundai.hyundaican import create_lkas11, create_clu11, create_lfahda_mfc, create_acc_commands, create_acc_opt, create_frt_radar_opt, create_mdps12, create_hda_mfc, create_scc12
from selfdrive.car.hyundai.values import Buttons, CarControllerParams, CAR, FEATURES
from opendbc.can.packer import CANPacker

from selfdrive.car.hyundai.navicontrol  import NaviControl

VisualAlert = car.CarControl.HUDControl.VisualAlert
LongCtrlState = car.CarControl.Actuators.LongControlState
LaneChangeState = log.LateralPlan.LaneChangeState

import common.loger as trace1

class CarController():
  def __init__(self, dbc_name, CP, VM):
    self.CP = CP
    self.params = CarControllerParams(CP)
    self.packer = CANPacker(dbc_name)
    self.frame = 0

    self.apply_steer_last = 0
    self.car_fingerprint = CP.carFingerprint
    self.steer_rate_limited = False
    self.last_resume_frame = 0
    self.accel = 0

    self.resume_cnt = 0
    self.last_lead_distance = 0
    self.lkas11_cnt = 0
    self.scc12_cnt = 0
    self.NC = NaviControl(self.params)
    self.debug_button = 0
    self.cut_in_car_alert = False
    self.cut_in_car_time = 0
    

    # hud
    self.hud_timer_alert = 0
    self.hud_timer_left = 0
    self.hud_timer_right = 0
    self.steer_timer_apply_torque = 1.0
    self.DT_STEER = 0.005             # 0.01 1sec, 0.005  2sec
    self.scc_live = not CP.radarOffCan



  def process_hud_alert(self, enabled, c, CS ):
    visual_alert = c.hudControl.visualAlert
    left_lane = c.hudControl.leftLaneVisible
    right_lane = c.hudControl.rightLaneVisible


    if CS.out.steeringPressed:
      self.hud_timer_alert = 0
    elif CS.clu_Vanz > 30:
      sys_warning = (visual_alert in (VisualAlert.steerRequired, VisualAlert.ldw))

      if sys_warning:
        self.hud_timer_alert = 500

    if left_lane:
      self.hud_timer_left = 100

    if right_lane:
      self.hud_timer_right = 100

    if self.hud_timer_alert:
      sys_warning = True
      self.hud_timer_alert -= 1
    else:
      sys_warning = False


    if self.hud_timer_left:
      self.hud_timer_left -= 1
 
    if self.hud_timer_right:
      self.hud_timer_right -= 1


    # initialize to no line visible
    sys_state = 1
    if self.hud_timer_left and self.hud_timer_right or sys_warning:  # HUD alert only display when LKAS status is active
      if enabled: # or sys_warning:
        sys_state = 3
      else:
        sys_state = 4
    elif self.hud_timer_left:
      sys_state = 5
    elif self.hud_timer_right:
      sys_state = 6

    return sys_warning, sys_state


  
  def smooth_steer( self, apply_torque, CS ):
    if self.CP.smoothSteer.maxSteeringAngle and abs(CS.out.steeringAngleDeg) > self.CP.smoothSteer.maxSteeringAngle:
      if self.CP.smoothSteer.maxDriverAngleWait and CS.out.steeringPressed:
        self.steer_timer_apply_torque -= self.CP.smoothSteer.maxDriverAngleWait # 0.002 #self.DT_STEER   # 0.01 1sec, 0.005  2sec   0.002  5sec
      elif self.CP.smoothSteer.maxSteerAngleWait:
        self.steer_timer_apply_torque -= self.CP.smoothSteer.maxSteerAngleWait # 0.001  # 10 sec
    elif self.CP.smoothSteer.driverAngleWait and CS.out.steeringPressed:
      self.steer_timer_apply_torque -= self.CP.smoothSteer.driverAngleWait #0.001
    else:
      if self.steer_timer_apply_torque >= 1:
          return int(round(float(apply_torque)))
      self.steer_timer_apply_torque += self.DT_STEER

    if self.steer_timer_apply_torque < 0:
      self.steer_timer_apply_torque = 0
    elif self.steer_timer_apply_torque > 1:
      self.steer_timer_apply_torque = 1

    apply_torque *= self.steer_timer_apply_torque

    return  int(round(float(apply_torque)))

  def cutin_detect(self, CS):
    cut_in, drel, d_rel1, d_rel2 = self.NC.get_cut_in_car()
    if drel < 40 and abs(cut_in) >= 3:
      self.cut_in_car_time = 100

    if self.cut_in_car_time > 1:
      self.cut_in_car_time -= 1      
      self.cut_in_car_alert = True
    else:
      self.cut_in_car_alert = False


  def update_debug(self, CS, c, apply_steer ):
    actuators = c.actuators
    vFuture = c.hudControl.vFuture * 3.6
    str_log1 = 'MODE={:.0f} vF={:.1f}  DIST={:.2f}'.format( CS.cruise_set_mode, vFuture, CS.lead_distance )
    trace1.printf2( '{}'.format( str_log1 ) )


    str_log1 = 'TG={:.1f}   aRV={:.2f}'.format( apply_steer,  CS.aReqValue  )
    trace1.printf3( '{}'.format( str_log1 ) )
  

  def updateLongitudinal(self, can_sends,  c, CS):
    enabled = c.enabled and CS.out.cruiseState.accActive
    actuators = c.actuators
    hud_speed = c.hudControl.setSpeed

    if self.frame % 2 == 0:
      lead_visible = False
      accel = actuators.accel if enabled else 0

      jerk = clip(2.0 * (accel - CS.out.aEgo), -12.7, 12.7)

      if accel < 0:
        accel = interp(accel - CS.out.aEgo, [-1.0, -0.5], [2 * accel, accel])

      accel = clip(accel, CarControllerParams.ACCEL_MIN, CarControllerParams.ACCEL_MAX)

      stopping = (actuators.longControlState == LongCtrlState.stopping)
      set_speed_in_units = hud_speed * (CV.MS_TO_MPH if CS.clu11["CF_Clu_SPEED_UNIT"] == 1 else CV.MS_TO_KPH)
      can_sends.extend(create_acc_commands(self.packer, enabled, accel, jerk, int(self.frame / 2), lead_visible, set_speed_in_units, stopping, CS.out.gasPressed))
      self.accel = accel

    # 5 Hz ACC options
    if self.frame % 20 == 0:
      can_sends.extend(create_acc_opt(self.packer))

    # 2 Hz front radar options
    if self.frame % 50 == 0:
      can_sends.append(create_frt_radar_opt(self.packer))      

    return  can_sends

  
  def update_scc12(self, can_sends,  c, CS ):
    actuators = c.actuators
    enabled = c.enabled and CS.out.cruiseState.accActive
    accel = actuators.accel if enabled else 0
    #if accel < 0:
    #  accel = interp(accel - CS.out.aEgo, [-1.0, -0.5], [2 * accel, accel])
    accel = clip(accel, CarControllerParams.ACCEL_MIN, CarControllerParams.ACCEL_MAX)

    if (CS.aReqValue > accel)  and CS.clu_Vanz > 3:
      #can_sends.append( create_scc12(self.packer, accel, enabled, self.scc12_cnt, self.scc_live, CS.scc12 ) )
      pass
    else:
      accel = CS.aReqValue

    can_sends.append( create_scc12(self.packer, accel, enabled, self.scc12_cnt, self.scc_live, CS.scc12 ) )
    self.accel = accel
    return can_sends    


  def update_resume(self, can_sends,  c, CS, path_plan):
    pcm_cancel_cmd = c.cruiseControl.cancel
    if pcm_cancel_cmd:
      can_sends.append(create_clu11(self.packer, self.frame, CS.clu11, Buttons.CANCEL))
    elif CS.out.cruiseState.standstill:
      if not self.CP.opkrAutoResume:
        self.last_lead_distance = 0
      elif CS.lead_distance < 4:
        self.last_lead_distance = 0
      elif CS.out.gasPressed:
        self.last_lead_distance = 0
      # run only first time when the car stopped
      elif self.last_lead_distance == 0:  
        # get the lead distance from the Radar
        self.last_lead_distance = CS.lead_distance
        self.resume_cnt = 0
      # when lead car starts moving, create 6 RES msgs
      elif CS.lead_distance != self.last_lead_distance and (self.frame - self.last_resume_frame) > 5:
        can_sends.append(create_clu11(self.packer, self.resume_cnt, CS.clu11, Buttons.RES_ACCEL))
        self.resume_cnt += 1
        # interval after 6 msgs
        if self.resume_cnt > 5:
          self.last_resume_frame = self.frame
          self.resume_cnt = 0
    # reset lead distnce after the car starts moving          
    elif self.last_lead_distance != 0:
      self.last_lead_distance = 0

    elif CS.out.cruiseState.accActive:
      btn_signal = self.NC.update( c, CS, path_plan )
      if btn_signal != None:
        self.debug_button = btn_signal
        can_sends.append(create_clu11(self.packer, self.resume_cnt, CS.clu11, btn_signal ))
        self.resume_cnt += 1
      else:
        self.debug_button = 0
        self.resume_cnt = 0

    return  can_sends


  def update(self, c, CS ):
    enabled = c.enabled
    active = c.latActive
    actuators = c.actuators
    left_lane = c.hudControl.leftLaneVisible 
    right_lane = c.hudControl.rightLaneVisible 
    left_lane_warning = c.hudControl.leftLaneDepart 
    right_lane_warning = c.hudControl.rightLaneDepart
    # pcm_cancel_cmd = c.cruiseControl.cancel    
    # vFuture = c.hudControl.vFuture * 3.6
  
    # Steering Torque
    new_steer = int(round(actuators.steer * self.params.STEER_MAX))
    apply_steer = apply_std_steer_torque_limits(new_steer, self.apply_steer_last, CS.out.steeringTorque, self.params)
    self.steer_rate_limited = new_steer != apply_steer

    if not self.steer_rate_limited:
      if CS.out.vEgo < 5:
        self.steer_rate_limited = True
      elif self.steer_timer_apply_torque < 1:
        self.steer_rate_limited = True


    if CS.engage_enable and not enabled:
      CS.engage_enable = False

    # disable when temp fault is active, or below LKA minimum speed
    # lkas_active = enabled and not CS.out.steerFaultTemporary and CS.out.vEgo >= self.CP.minSteerSpeed and CS.out.cruiseState.enabled
    path_plan = self.NC.update_lateralPlan()
    if path_plan.laneChangeState == LaneChangeState.laneChangeDisEngage:
      active = False
      self.cut_in_car_time = 0
      self.cut_in_car_alert = False
    else:
      self.cutin_detect( CS )

    lkas_active = enabled and active and not CS.out.steerFaultTemporary and  CS.out.vEgo >= self.CP.minSteerSpeed and CS.out.cruiseState.enabled


    if not lkas_active:
      apply_steer = 0
      self.steer_timer_apply_torque = 0
    else:
      apply_steer = self.smooth_steer( apply_steer, CS )

    apply_steer = clip( apply_steer, -self.params.STEER_MAX, self.params.STEER_MAX )
    self.apply_steer_last = apply_steer
    sys_warning, sys_state = self.process_hud_alert( lkas_active, c, CS )


    if self.frame == 0: # initialize counts from last received count signals
      self.lkas11_cnt = CS.lkas11["CF_Lkas_MsgCount"] + 1
      self.scc12_cnt = CS.scc12["CR_VSM_Alive"] + 1
  
    self.lkas11_cnt %= 0x10
    self.scc12_cnt %= 0x0F

    # tester present - w/ no response (keeps radar disabled)

    can_sends = []
    if self.CP.openpilotLongitudinalControl:
      if self.frame % 100 == 0:
        can_sends.append([0x7D0, 0, b"\x02\x3E\x80\x00\x00\x00\x00\x00", 0])


    can_sends.append( create_lkas11(self.packer, self.lkas11_cnt, self.car_fingerprint, apply_steer, lkas_active,
                                   CS.lkas11, sys_warning, sys_state, enabled,
                                   left_lane, right_lane,
                                   left_lane_warning, right_lane_warning) )

    can_sends.append( create_mdps12(self.packer, self.frame, CS.mdps12) )

    if  self.CP.openpilotLongitudinalControl:
      self.updateLongitudinal( can_sends, c, CS )
    else:
      self.update_resume( can_sends, c, CS, path_plan )

    if self.CP.atompilotLongitudinalControl:
      if (self.frame % 2 == 0) and CS.cruise_set_mode == 2:
        self.update_scc12( can_sends, c, CS )
        self.scc12_cnt += 1
    else:
      self.accel = CS.aReqValue
      
    # 20 Hz LFA MFA message
    if self.frame % 5 == 0:
      self.update_debug( CS, c, apply_steer )
      if self.car_fingerprint in FEATURES["send_hda_mfa"]:
        can_sends.append( create_hda_mfc(self.packer, CS, c ) )
      elif self.car_fingerprint in FEATURES["send_lfa_mfa"]:
        can_sends.append( create_lfahda_mfc(self.packer, enabled) )
   

    new_actuators = actuators.copy()
    new_actuators.steer = apply_steer / self.params.STEER_MAX
    new_actuators.accel = self.accel

    self.lkas11_cnt += 1
    self.frame += 1
    return new_actuators, can_sends
